"""This module defines LoopPulseTemplate, a higher-order hierarchical pulse template that loops
another PulseTemplate based on a condition."""


from typing import Dict, Set, Optional, Any, Union, Tuple, Iterator, Sequence, cast, Mapping
import warnings
from numbers import Number

import sympy
from cached_property import cached_property

from qupulse.serialization import Serializer, PulseRegistryType
from qupulse.parameter_scope import Scope, MappedScope, DictScope
from qupulse.utils.types import FrozenDict

from qupulse._program._loop import Loop

from qupulse.expressions import ExpressionScalar, ExpressionVariableMissingException
from qupulse.utils import checked_int_cast
from qupulse.pulses.parameters import InvalidParameterNameException, ParameterConstrainer, ParameterNotProvidedException
from qupulse.pulses.pulse_template import PulseTemplate, ChannelID, BuildRequirement, BuildContext
from qupulse._program.waveforms import SequenceWaveform as ForLoopWaveform
from qupulse.pulses.measurement import MeasurementDefiner, MeasurementDeclaration

__all__ = ['ForLoopPulseTemplate', 'LoopPulseTemplate', 'LoopIndexNotUsedException']


class LoopPulseTemplate(PulseTemplate):
    """Base class for loop based pulse templates. This class is still abstract and cannot be instantiated."""
    def __init__(self, body: PulseTemplate,
                 identifier: Optional[str]):
        super().__init__(identifier=identifier)
        self.__body = body

    @property
    def body(self) -> PulseTemplate:
        return self.__body

    @property
    def defined_channels(self) -> Set['ChannelID']:
        return self.__body.defined_channels

    @property
    def measurement_names(self) -> Set[str]:
        return self.__body.measurement_names


class ParametrizedRange:
    """Like the builtin python range but with parameters."""
    def __init__(self, *args, **kwargs):
        """Positional and keyword arguments cannot be mixed.

        Args:
            *args: Interpreted as ``(start, )`` or ``(start, stop[, step])``
            **kwargs: Expected to contain ``start``, ``stop`` and ``step``
        Raises:
            TypeError: If positional and keyword arguments are mixed
            KeyError: If keyword arguments but one of ``start``, ``stop`` or ``step`` is missing
        """
        if args and kwargs:
            raise TypeError('ParametrizedRange only takes either positional or keyword arguments')
        elif kwargs:
            start = kwargs['start']
            stop = kwargs['stop']
            step = kwargs['step']
        elif len(args) in (1, 2, 3):
            if len(args) == 3:
                start, stop, step = args
            elif len(args) == 2:
                (start, stop), step = args, 1
            elif len(args) == 1:
                start, (stop,), step = 0, args, 1
        else:
            raise TypeError('ParametrizedRange expected 1 to 3 arguments, got {}'.format(len(args)))

        self.start = ExpressionScalar.make(start)
        self.stop = ExpressionScalar.make(stop)
        self.step = ExpressionScalar.make(step)

    def to_tuple(self) -> Tuple[Any, Any, Any]:
        """Return a simple representation of the range which is useful for comparison and serialization"""
        return (self.start.get_serialization_data(),
                self.stop.get_serialization_data(),
                self.step.get_serialization_data())

    def to_range(self, parameters: Mapping[str, Number]) -> range:
        return range(checked_int_cast(self.start.evaluate_in_scope(parameters)),
                     checked_int_cast(self.stop.evaluate_in_scope(parameters)),
                     checked_int_cast(self.step.evaluate_in_scope(parameters)))

    @property
    def parameter_names(self) -> Set[str]:
        return set(self.start.variables) | set(self.stop.variables) | set(self.step.variables)


class ForLoopPulseTemplate(LoopPulseTemplate, MeasurementDefiner, ParameterConstrainer):
    """This pulse template allows looping through an parametrized integer range and provides the loop index as a
    parameter to the body. If you do not need the index in the pulse template, consider using
    :class:`~qupulse.pulses.repetition_pulse_template.RepetitionPulseTemplate`"""
    def __init__(self,
                 body: PulseTemplate,
                 loop_index: str,
                 loop_range: Union[int,
                                   range,
                                   str,
                                   Tuple[Any, Any],
                                   Tuple[Any, Any, Any],
                                   ParametrizedRange],
                 identifier: Optional[str]=None,
                 *,
                 measurements: Optional[Sequence[MeasurementDeclaration]]=None,
                 parameter_constraints: Optional[Sequence]=None,
                 registry: PulseRegistryType=None) -> None:
        """
        Args:
            body: The loop body. It is expected to have `loop_index` as an parameter
            loop_index: Loop index of the for loop
            loop_range: Range to loop through
            identifier: Used for serialization
        """
        LoopPulseTemplate.__init__(self, body=body, identifier=identifier)
        MeasurementDefiner.__init__(self, measurements=measurements)
        ParameterConstrainer.__init__(self, parameter_constraints=parameter_constraints)

        if isinstance(loop_range, ParametrizedRange):
            self._loop_range = loop_range
        elif isinstance(loop_range, (int, str)):
            self._loop_range = ParametrizedRange(loop_range)
        elif isinstance(loop_range, (tuple, list)):
            self._loop_range = ParametrizedRange(*loop_range)
        elif isinstance(loop_range, range):
            self._loop_range = ParametrizedRange(start=loop_range.start,
                                                 stop=loop_range.stop,
                                                 step=loop_range.step)
        else:
            raise ValueError('loop_range is not valid')

        if not loop_index.isidentifier():
            raise InvalidParameterNameException(loop_index)
        body_parameters = self.body.parameter_names
        if loop_index not in body_parameters:
            raise LoopIndexNotUsedException(loop_index, body_parameters)
        self._loop_index = loop_index

        if self.loop_index in self.constrained_parameters:
            constraints = [str(constraint) for constraint in self.parameter_constraints
                           if self._loop_index in constraint.affected_parameters]
            warnings.warn("ForLoopPulseTemplate was created with a constraint on a variable shadowing the loop index.\n" \
                          "This will not constrain the actual loop index but introduce a new parameter.\n" \
                          "To constrain the loop index, put the constraint in the body subtemplate.\n" \
                          "Loop index is {} and offending constraints are: {}".format(self._loop_index, constraints))

        self._register(registry=registry)

    @property
    def loop_index(self) -> str:
        return self._loop_index

    @property
    def loop_range(self) -> ParametrizedRange:
        return self._loop_range

    @property
    def measurement_names(self) -> Set[str]:
        return LoopPulseTemplate.measurement_names.fget(self) | MeasurementDefiner.measurement_names.fget(self)

    @cached_property
    def duration(self) -> ExpressionScalar:
        step_size = self._loop_range.step.sympified_expression
        loop_index = sympy.symbols(self._loop_index)
        sum_index = sympy.symbols(self._loop_index)

        # replace loop_index with sum_index dependable expression
        body_duration = self.body.duration.sympified_expression.subs({loop_index: self._loop_range.start.sympified_expression + sum_index*step_size})

        # number of sum contributions
        step_count = sympy.ceiling((self._loop_range.stop.sympified_expression-self._loop_range.start.sympified_expression) / step_size)
        sum_start = 0
        sum_stop = sum_start + (sympy.functions.Max(step_count, 1) - 1)

        # expression used if step_count >= 0
        finite_duration_expression = sympy.Sum(body_duration, (sum_index, sum_start, sum_stop))

        duration_expression = sympy.Piecewise((0, step_count <= 0),
                                              (finite_duration_expression, True))

        return ExpressionScalar(duration_expression)

    @property
    def parameter_names(self) -> Set[str]:
        parameter_names = self.body.parameter_names.copy()
        parameter_names.remove(self._loop_index)
        return parameter_names | self._loop_range.parameter_names | self.constrained_parameters | self.measurement_parameters

    def _body_scope_generator(self, scope: Scope, forward=True) -> Iterator[Scope]:
        loop_range = self._loop_range.to_range(scope)

        loop_range = loop_range if forward else reversed(loop_range)
        loop_index_name = self._loop_index

        for loop_index_value in loop_range:
            yield scope.overwrite({loop_index_name: loop_index_value})

    def _internal_create_program(self, *,
                                 scope: Scope,
                                 measurement_mapping: Dict[str, Optional[str]],
                                 channel_mapping: Dict[ChannelID, Optional[ChannelID]],
                                 global_transformation: Optional['Transformation'],
                                 to_single_waveform: Set[Union[str, 'PulseTemplate']],
                                 parent_loop: Loop) -> None:
        self.validate_scope(scope=scope)

        try:
            duration = self.duration.evaluate_in_scope(scope)
        except ExpressionVariableMissingException as err:
            raise ParameterNotProvidedException(err.variable) from err

        if duration > 0:
            measurements = self.get_measurement_windows(scope, measurement_mapping)
            if measurements:
                parent_loop.add_measurements(measurements)

            for local_scope in self._body_scope_generator(scope, forward=True):
                self.body._create_program(scope=local_scope,
                                          measurement_mapping=measurement_mapping,
                                          channel_mapping=channel_mapping,
                                          global_transformation=global_transformation,
                                          to_single_waveform=to_single_waveform,
                                          parent_loop=parent_loop)

    def build_waveform(self, parameter_scope: Scope) -> ForLoopWaveform:
        return ForLoopWaveform([self.body.build_waveform(local_scope)
                                for local_scope in self._body_scope_generator(parameter_scope, forward=True)])

    def get_serialization_data(self, serializer: Optional[Serializer]=None) -> Dict[str, Any]:
        data = super().get_serialization_data(serializer)

        data['body'] = self.body

        if serializer: # compatibility to old serialization routines, deprecated
            data = dict()
            data['body'] = serializer.dictify(self.body)

        data['loop_range'] = self._loop_range.to_tuple()
        data['loop_index'] = self._loop_index

        if self.parameter_constraints:
            data['parameter_constraints'] = [str(c) for c in self.parameter_constraints]
        if self.measurement_declarations:
            data['measurements'] = self.measurement_declarations

        return data

    @classmethod
    def deserialize(cls, serializer: Optional[Serializer]=None, **kwargs) -> 'ForLoopPulseTemplate':
        if serializer: # compatibility to old serialization routines, deprecated
            kwargs['body'] = cast(PulseTemplate, serializer.deserialize(kwargs['body']))
        return super().deserialize(None, **kwargs)

    @property
    def integral(self) -> Dict[ChannelID, ExpressionScalar]:

        step_size = self._loop_range.step.sympified_expression
        loop_index = sympy.symbols(self._loop_index)
        sum_index = sympy.symbols(self._loop_index)

        body_integrals = self.body.integral
        body_integrals = {
            c: body_integrals[c].sympified_expression.subs(
                {loop_index: self._loop_range.start.sympified_expression + sum_index*step_size}
            )
            for c in body_integrals
        }

        # number of sum contributions
        step_count = sympy.ceiling((self._loop_range.stop.sympified_expression-self._loop_range.start.sympified_expression) / step_size)
        sum_start = 0
        sum_stop = sum_start + (sympy.functions.Max(step_count, 1) - 1)

        for c in body_integrals:
            channel_integral_expr = sympy.Sum(body_integrals[c], (sum_index, sum_start, sum_stop))
            body_integrals[c] = ExpressionScalar(channel_integral_expr)

        return body_integrals

    @property
    def initial_values(self) -> Dict[ChannelID, ExpressionScalar]:
        values = self.body.initial_values
        initial_idx = self._loop_range.start
        for ch, value in values.items():
            values[ch] = ExpressionScalar(value.underlying_expression.subs(self._loop_index, initial_idx))
        return values

    @property
    def final_values(self) -> Dict[ChannelID, ExpressionScalar]:
        values = self.body.initial_values
        start, step, stop = self._loop_range.start.sympified_expression, self._loop_range.step.sympified_expression, self._loop_range.stop.sympified_expression
        n = (stop - start) // step
        final_idx = start + sympy.Max(n - 1, 0) * step
        for ch, value in values.items():
            values[ch] = ExpressionScalar(value.underlying_expression.subs(self._loop_index, final_idx))
        return values

    def required_context(self) -> BuildRequirement:
        required = self.body.required_context()
        if required.previous or required.next:
            raise NotImplementedError("ForLoopPulseTemplates do not support reference to previous or next pulse"
                                      "templates yet")
        return required

    def build(self, context: BuildContext) -> PulseTemplate:
        with context.with_parent(self) as inner_context:
            serialized = self.get_serialization_data()
            serialized['body'] = self.body.build(inner_context)
        return ForLoopPulseTemplate(**serialized, registry=context.pulse_registry)


class LoopIndexNotUsedException(Exception):
    def __init__(self, loop_index: str, body_parameter_names: Set[str]):
        self.loop_index = loop_index
        self.body_parameter_names = body_parameter_names

    def __str__(self) -> str:
        return "The parameter {} is missing in the body's parameter names: {}".format(self.loop_index,
                                                                                      self.body_parameter_names)
