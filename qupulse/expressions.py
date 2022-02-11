"""
This module defines the class Expression to represent mathematical expression as well as
corresponding exception classes.
"""
import math
import numbers
from typing import Any, Dict, Union, Sequence, Callable, TypeVar, Type, Mapping, Tuple
from numbers import Number
import warnings
import functools
import array
import itertools
import dataclasses

import sympy
import numpy

try:
    import symengine
except ImportError:
    symengine = None

from abc import ABCMeta, abstractmethod
from qupulse.serialization import AnonymousSerializable
from qupulse.utils.sympy import sympify, to_numpy, recursive_substitution, evaluate_lambdified,\
    get_most_simple_representation, get_variables, evaluate_lamdified_exact_rational, _lambdify_modules
from qupulse.utils.types import TimeType

__all__ = ["Expression", "ExpressionVariableMissingException", "ExpressionScalar", "ExpressionVector", "ExpressionLike"]


_ExpressionType = TypeVar('_ExpressionType', bound='Expression')


class _ExpressionMeta(type):
    """Metaclass that forwards calls to Expression(...) to Expression.make(...) to make subclass objects"""
    def __call__(cls: Type[_ExpressionType], *args, **kwargs) -> _ExpressionType:
        if cls is Expression:
            return cls.make(*args, **kwargs)
        else:
            return type.__call__(cls, *args, **kwargs)


class _ExpressionMeta2(ABCMeta):
    """Metaclass that forwards calls to Expression(...) to Expression.make(...) to make subclass objects"""
    def __call__(cls: Type[_ExpressionType], *args, **kwargs) -> _ExpressionType:
        return _make(cls, *args, **kwargs)


if symengine is None:
    _to_symbolic_float = sympy.Float
    _to_symbolic_int = sympy.Integer
else:
    _to_symbolic_float = symengine.RealDouble
    _to_symbolic_int = symengine.Integer


def _make(cls,
         expression_or_dict) -> Union['ExpressionScalar', 'ExpressionVector']:
    """Backward compatible expression generation"""
    if isinstance(expression_or_dict, dict):
        expression = expression_or_dict['expression']
    elif isinstance(expression_or_dict, cls):
        return expression_or_dict
    else:
        expression = expression_or_dict

    if cls is ExpressionVector:
        return ExpressionVector(expression)
    if cls is ExpressionScalar:
        return ExpressionScalar(expression)
    else:
        if isinstance(expression, (list, tuple, numpy.ndarray, sympy.NDimArray, array.array)):
            return ExpressionVector(expression)
        else:
            return ExpressionScalar(expression)


def _parse_evaluate_numeric_arguments(expression, eval_args: Mapping[str, Number]) -> Tuple[Any, ...]:
    try:
        return tuple(eval_args[v] for v in expression.variables)
    except KeyError as key_error:
        if type(key_error).__module__.startswith('qupulse'):
            # we forward qupulse errors
            raise
        else:
            raise ExpressionVariableMissingException(key_error.args[0], expression) from key_error


def _parse_evaluate_numeric_scalar(expression,
                                   result: Union[Number, numpy.ndarray],
                                   call_arguments: Any) -> Union[Number, numpy.ndarray]:
    allowed_types = (float, numpy.number, int, complex, bool, numpy.bool_, TimeType)
    if isinstance(result, (tuple, list)):
        if len(result) > 1:
            result = numpy.asarray(result)
        else:
            result, = result
    if isinstance(result, numpy.ndarray):
        result = numpy.squeeze(result)
        if result.shape == ():
            result = result[()]
        elif issubclass(result.dtype.type, allowed_types):
            return result
        obj_types = set(map(type, result.flat))
        if all(issubclass(obj_type, numbers.Integral) for obj_type in obj_types):
            return result.astype(numpy.int64)
        if all(issubclass(obj_type, (numbers.Real, numbers.Integral)) for obj_type in obj_types):
            return result.astype(numpy.float64)
        if all(issubclass(obj_type, numbers.Rational) for obj_type in obj_types):
            return result.astype(TimeType)
        else:
            raise NonNumericEvaluation(expression, result, call_arguments)

    if isinstance(result, allowed_types):
        return result

    elif isinstance(result, (sympy.Integral, symengine.Integer)):
        return int(result)
    elif isinstance(result, (sympy.Rational, symengine.Rational)):
        return TimeType(result)
    elif isinstance(result, (sympy.Float, symengine.RealNumber)):
        return float(result)
    else:
        raise NonNumericEvaluation(expression, result, call_arguments)


class Expression2(metaclass=_ExpressionMeta2):
    """This is just an interface class."""

    def __call__(cls: Type[_ExpressionType], *args, **kwargs) -> _ExpressionType:
        return cls.make(*args, **kwargs)

    def __init__(self, *args, **kwargs):
        raise NotImplementedError()

    @classmethod
    def make(cls, expression_or_dict) -> Union['ExpressionScalar', 'ExpressionVector']:
        """Backward compatible expression generation"""
        return _make(cls, expression_or_dict)

    @abstractmethod
    def evaluate_in_scope(self, scope: Mapping) -> Union[Number, numpy.ndarray]:
        """Evaluate the expression by taking the variables from the given scope (typically of type Scope but it can be
        any mapping.)
        Args:
            scope:

        Returns:

        """

    @abstractmethod
    def evaluate_numeric(self, **kwargs) -> Union[Number, numpy.ndarray]:
        """TODO: deprecation warning?"""

    @abstractmethod
    def evaluate_symbolic(self, substitutions: Mapping[Any, Any]) -> 'Expression':
        """"""

    @property
    @abstractmethod
    def variables(self) -> Sequence[str]:
        """ Get all free variables in the expression.

        Returns:
            A collection of all free variables occurring in the expression.
        """


Expression = Expression2


class ExpressionScalar2:
    __slots__ = (
        '_original_expression',
        '_sympified_expression',
        '_variables',
        '_expression_lambdified',
        '_exact_rational_lambdified',
    )

    _SYMBOLIC_TYPES = (sympy.Expr, symengine.Expr)
    _NUMERIC_TYPES = (int, float)

    def __init__(self, ex: Union[str, Number, sympy.Expr]):
        self._expression_lambdified = None
        self._exact_rational_lambdified = None

        if isinstance(ex, self._NUMERIC_TYPES):
            self._original_expression = ex
            self._sympified_expression = None
            self._variables = ()

        elif isinstance(ex, self._SYMBOLIC_TYPES):
            self._original_expression = None
            self._sympified_expression = ex
            self._variables = None

        elif isinstance(ex, ExpressionScalar2):
            self._original_expression = ex._original_expression
            self._sympified_expression = ex._sympified_expression
            self._variables = ex._variables
            self._expression_lambdified = ex._expression_lambdified
            self._exact_rational_lambdified = ex._exact_rational_lambdified

        else:
            self._original_expression = ex
            self._sympified_expression = sympify(ex)
            self._variables = None

    @classmethod
    def make(cls, expression_or_dict):
        return _make(cls, expression_or_dict)

    def evaluate_in_scope(self, scope: Mapping) -> Union[Number, numpy.ndarray]:
        if self._sympified_expression is None:
            return self._original_expression
        elif symengine and isinstance(self._sympified_expression, symengine.Expr):
            result = self._sympified_expression.subs(scope)
        else:
            args = _parse_evaluate_numeric_arguments(self, scope)
            lambified = self._expression_lambdified or sympy.lambdify(self.variables,
                                                                      self._sympified_expression,
                                                                      _lambdify_modules)
            self._expression_lambdified = lambified
            result = lambified(*args)
        return _parse_evaluate_numeric_scalar(self, result, scope)

    def evaluate_numeric(self, **kwargs) -> Union[Number, numpy.ndarray]:
        return self.evaluate_in_scope(kwargs)

    def evaluate_symbolic(self, substitutions: Mapping[Any, Any]) -> 'ExpressionScalar2':
        if self._sympified_expression is None or not substitutions:
            return self
        else:
            # find a way to use self._sympified_expression.subs(substitutions) if it works
            return ExpressionScalar2(recursive_substitution(self._sympified_expression, substitutions))

    def evaluate_with_exact_rationals(self, scope: Mapping) -> Number:
        if self._sympified_expression is None:
            return self._original_expression

        args = _parse_evaluate_numeric_arguments(self, scope)
        result, self._exact_rational_lambdified = evaluate_lamdified_exact_rational(self._sympified_expression,
                                                                                    self.variables,
                                                                                    args, {},
                                                                                    self._exact_rational_lambdified)
        return _parse_evaluate_numeric_scalar(self, result, scope)

    @property
    def variables(self) -> Sequence[str]:
        if self._variables is None:
            self._variables = get_variables(self._sympified_expression)
        return self._variables

    @property
    def sympified_expression(self):
        if self._sympified_expression is None:
            if isinstance(self._original_expression, float):
                return _to_symbolic_float(self._original_expression)
            else:
                return _to_symbolic_int(self._original_expression)
        else:
            return self._sympified_expression

    @property
    def original_expression(self) -> Union[str, Number]:
        if self._original_expression is None:
            return str(self._sympified_expression)
        else:
            return self._original_expression

    def get_serialization_data(self) -> Union[str, float, int]:
        if self._sympified_expression is None:
            return self._original_expression
        else:
            serialized = get_most_simple_representation(self._sympified_expression)
            if isinstance(serialized, str):
                return self.original_expression
            else:
                return serialized

    def is_nan(self) -> bool:
        if self._sympified_expression is None:
            return math.isnan(self._original_expression)
        else:
            return sympy.sympify('nan') == self._sympified_expression

    underlying_expression = sympified_expression

    def __hash__(self):
        return hash(self._sympified_expression or self._original_expression)

    def __eq__(self, other: 'ExpressionScalar2'):
        if self.__slots__ != getattr(other, '__slots__', None):
            if isinstance(other, numbers.Number):
                if self._sympified_expression is None:
                    return self._original_expression == other
                else:
                    return self._sympified_expression == other
            if isinstance(other, (sympy.Expr, symengine.Expr)):
                return self.sympified_expression == other
            elif isinstance(other, str):
                return self.sympified_expression == sympify(other)
            return NotImplemented
        else:
            if self._sympified_expression is None and other._sympified_expression is None:
                return self._original_expression == other._original_expression
            else:
                return self.sympified_expression == other.sympified_expression

    def __float__(self):
        if self._sympified_expression is None:
            return float(self._original_expression)
        else:
            return float(self._sympified_expression)

    def __str__(self) -> str:
        if self._sympified_expression is None:
            return str(self._original_expression)
        else:
            return str(self._sympified_expression)

    def __repr__(self) -> str:
        if self._original_expression is None:
            return f"ExpressionScalar('{self._sympified_expression!r}')"
        else:
            return f"ExpressionScalar({self._original_expression!r})"

    def __format__(self, format_spec):
        if format_spec == '':
            return str(self)
        return format(float(self), format_spec)

    def __lt__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self.sympified_expression < type(self)(other).sympified_expression
        return bool(result) if result.is_Boolean else None

    def __gt__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self.sympified_expression > type(self)(other).sympified_expression
        return bool(result) if result.is_Boolean else None

    def __ge__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self.sympified_expression >= type(self)(other).sympified_expression
        return bool(result) if result.is_Boolean else None

    def __le__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self.sympified_expression <= type(self)(other).sympified_expression
        return bool(result) if result.is_Boolean else None

    def __add__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__add__(sympify(other)))

    def __radd__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(sympify(other).__radd__(self.sympified_expression))

    def __sub__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__sub__(sympify(other)))

    def __rsub__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__rsub__(sympify(other)))

    def __mul__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self._sympified_expression.__mul__(sympify(other)))

    def __rmul__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__rmul__(sympify(other)))

    def __truediv__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__truediv__(sympify(other)))

    def __rtruediv__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__rtruediv__(sympify(other)))

    def __neg__(self) -> 'ExpressionScalar2':
        return type(self)(self.sympified_expression.__neg__())

    def __pos__(self):
        return type(self)(self.sympified_expression.__pos__())

    def _sympy_(self):
        return self.sympified_expression


Expression2.register(ExpressionScalar2)


class ExpressionVector2:
    __slots__ = ('_expression_vector', '_variables', '_expression_lambda')
    """N-dimensional expression.
    TODO: write doc!
    TODO: write tests!
    """

    sympify_vector = numpy.vectorize(sympify)

    def __init__(self, expression_vector: Sequence):
        if isinstance(expression_vector, ExpressionVector):
            self._expression_vector = expression_vector._expression_vector
            self._expression_lambda = expression_vector._expression_lambda
            self._variables = expression_vector._variables
        if isinstance(expression_vector, sympy.NDimArray):
            expression_vector = to_numpy(expression_vector)
        self._expression_vector = self.sympify_vector(expression_vector)
        self._expression_lambda = None
        self._variables = None

    make = classmethod(_make)

    @property
    def variables(self) -> Sequence[str]:
        if self._variables is None:
            variables = set(itertools.chain.from_iterable(map(get_variables, self._expression_vector.flat)))
            self._variables = tuple(variables)
        return self._variables

    def evaluate_in_scope(self, scope: Mapping) -> Union[Number, numpy.ndarray]:
        parsed_args = _parse_evaluate_numeric_arguments(self, scope)

        self._expression_lambda = self._expression_lambda or sympy.lambdify(self.variables,
                                                                            self.underlying_expression,
                                                                            _lambdify_modules)
        result = self._expression_lambda(*parsed_args)

        return _parse_evaluate_numeric_scalar(self, numpy.array(result), scope)

    def evaluate_symbolic(self, substitutions: Mapping[Any, Any]) -> 'Expression':
        if substitutions:
            return Expression.make(recursive_substitution(sympy.NDimArray(self._expression_vector), substitutions))
        else:
            return self

    def evaluate_numeric(self, **kwargs) -> Union[numpy.ndarray, Number]:
        return self.evaluate_in_scope(kwargs)

    def get_serialization_data(self) -> Sequence[str]:
        def nested_get_most_simple_representation(list_or_expression):
            if isinstance(list_or_expression, list):
                return [nested_get_most_simple_representation(entry)
                        for entry in list_or_expression]
            else:
                return get_most_simple_representation(list_or_expression)
        return nested_get_most_simple_representation(self._expression_vector.tolist())

    def __str__(self):
        return str(self.get_serialization_data())

    def __repr__(self):
        return f'ExpressionVector({self.get_serialization_data()!r})'

    def _sympy_(self):
        return sympy.NDimArray(self._expression_vector)

    def __eq__(self, other):
        if not isinstance(other, Expression):
            other = Expression.make(other)
        if isinstance(other, ExpressionScalar):
            return self._expression_vector.size == 1 and self._expression_vector[0] == other.underlying_expression
        if isinstance(other, ExpressionVector) and self._expression_vector.shape != other._expression_vector.shape:
            return False
        return numpy.all(self._expression_vector == other.underlying_expression)

    def __getitem__(self, item) -> Expression:
        return self._expression_vector[item]

    @property
    def underlying_expression(self) -> numpy.ndarray:
        return self._expression_vector


Expression2.register(ExpressionVector2)


ExpressionScalar = ExpressionScalar2
ExpressionVector = ExpressionVector2


class ExpressionOld(metaclass=_ExpressionMeta):
    __slots__ = ('_expression_lambda', '_variables')

    """Base class for expressions."""
    def __init__(self, *args, **kwargs):
        self._expression_lambda = None
        self._variables = None

    def _parse_evaluate_numeric_arguments(self, eval_args: Mapping[str, Number]) -> Dict[str, Number]:
        try:
            return {v: eval_args[v] for v in self.variables}
        except KeyError as key_error:
            if type(key_error).__module__.startswith('qupulse'):
                # we forward qupulse errors, I down like this
                raise
            else:
                raise ExpressionVariableMissingException(key_error.args[0], self) from key_error

    def _parse_evaluate_numeric_result(self,
                                       result: Union[Number, numpy.ndarray],
                                       call_arguments: Any) -> Union[Number, numpy.ndarray]:
        allowed_types = (float, numpy.number, int, complex, bool, numpy.bool_, TimeType)
        if isinstance(result, tuple):
            result = numpy.array(result)
        if isinstance(result, numpy.ndarray):
            if issubclass(result.dtype.type, allowed_types):
                return result
            else:
                obj_types = set(map(type, result.flat))
                if all(issubclass(obj_type, sympy.Integer) for obj_type in obj_types):
                    return result.astype(numpy.int64)
                if all(issubclass(obj_type, (sympy.Integer, sympy.Float)) for obj_type in obj_types):
                    return result.astype(float)
                else:
                    raise NonNumericEvaluation(self, result, call_arguments)
        elif isinstance(result, allowed_types):
            return result
        elif isinstance(result, sympy.Float):
            return float(result)
        elif isinstance(result, sympy.Integer):
            return int(result)
        else:
            raise NonNumericEvaluation(self, result, call_arguments)

    def evaluate_in_scope(self, scope: Mapping) -> Union[Number, numpy.ndarray]:
        """Evaluate the expression by taking the variables from the given scope (typically of type Scope but it can be
        any mapping.)
        Args:
            scope:

        Returns:

        """
        if self._variables:
            parsed_kwargs = self._parse_evaluate_numeric_arguments(scope)

            result, self._expression_lambda = evaluate_lambdified(self.underlying_expression, self.variables,
                                                                  parsed_kwargs, lambdified=self._expression_lambda)
        else:
            result = self.underlying_expression

        return self._parse_evaluate_numeric_result(result, scope)

    def evaluate_numeric(self, **kwargs) -> Union[Number, numpy.ndarray]:
        return self.evaluate_in_scope(kwargs)

    def __float__(self):
        if self.variables:
            return NotImplemented
        else:
            e = self.evaluate_numeric()
            return float(e)

    def evaluate_symbolic(self, substitutions: Mapping[Any, Any]) -> 'Expression':
        if substitutions:
            return Expression.make(recursive_substitution(sympify(self.underlying_expression), substitutions))
        else:
            return self

    @property
    def variables(self) -> Sequence[str]:
        """ Get all free variables in the expression.

        Returns:
            A collection of all free variables occurring in the expression.
        """
        raise NotImplementedError()

    @classmethod
    def make(cls: Type[_ExpressionType],
             expression_or_dict,
             numpy_evaluation=None) -> Union['ExpressionScalar', 'ExpressionVector', _ExpressionType]:
        """Backward compatible expression generation"""
        if numpy_evaluation is not None:
            warnings.warn('numpy_evaluation keyword argument is deprecated and ignored.')

        if isinstance(expression_or_dict, dict):
            expression = expression_or_dict['expression']
        elif isinstance(expression_or_dict, cls):
            return expression_or_dict
        else:
            expression = expression_or_dict

        if cls is Expression:
            if isinstance(expression, (list, tuple, numpy.ndarray, sympy.NDimArray, array.array)):
                return ExpressionVector(expression)
            else:
                return ExpressionScalar(expression)
        else:
            return cls(expression)

    @property
    def underlying_expression(self) -> Union[sympy.Expr, numpy.ndarray]:
        raise NotImplementedError()


class ExpressionVectorOld(ExpressionOld):
    __slots__ = ('_expression_vector',)
    """N-dimensional expression.
    TODO: write doc!
    TODO: write tests!
    """
    sympify_vector = numpy.vectorize(sympify)

    def __init__(self, expression_vector: Sequence):
        super().__init__()
        if isinstance(expression_vector, sympy.NDimArray):
            expression_vector = to_numpy(expression_vector)
        self._expression_vector = self.sympify_vector(expression_vector)
        variables = set(itertools.chain.from_iterable(map(get_variables, self._expression_vector.flat)))
        self._variables = tuple(variables)

    @property
    def expression_lambda(self) -> Callable:
        if self._expression_lambda is None:
            expression_lambda = sympy.lambdify(self.variables, self.underlying_expression,
                                                     [{'ceiling': ceiling}, 'numpy'])

            @functools.wraps(expression_lambda)
            def expression_wrapper(*args, **kwargs):
                result = expression_lambda(*args, **kwargs)
                if isinstance(result, sympy.NDimArray):
                    return numpy.array(result.tolist())
                elif isinstance(result, list):
                    return numpy.array(result).reshape(self.underlying_expression.shape)
                else:
                    return result.reshape(self.underlying_expression.shape)
            self._expression_lambda = expression_wrapper
        return self._expression_lambda

    @property
    def variables(self) -> Sequence[str]:
        return self._variables

    def evaluate_numeric(self, **kwargs) -> Union[numpy.ndarray, Number]:
        parsed_kwargs = self._parse_evaluate_numeric_arguments(kwargs)

        result, self._expression_lambda = evaluate_lambdified(self.underlying_expression, self.variables,
                                                              parsed_kwargs, lambdified=self._expression_lambda)

        if isinstance(result, (list, tuple)):
            result = numpy.array(result)

        return self._parse_evaluate_numeric_result(numpy.array(result), kwargs)

    def get_serialization_data(self) -> Sequence[str]:
        def nested_get_most_simple_representation(list_or_expression):
            if isinstance(list_or_expression, list):
                return [nested_get_most_simple_representation(entry)
                        for entry in list_or_expression]
            else:
                return get_most_simple_representation(list_or_expression)
        return nested_get_most_simple_representation(self._expression_vector.tolist())

    def __str__(self):
        return str(self.get_serialization_data())

    def __repr__(self):
        return 'ExpressionVector({})'.format(repr(self.get_serialization_data()))

    def _sympy_(self):
        return sympy.NDimArray(self._expression_vector)

    def __eq__(self, other):
        if not isinstance(other, Expression):
            other = Expression.make(other)
        if isinstance(other, ExpressionScalar):
            return self._expression_vector.size == 1 and self._expression_vector[0] == other.underlying_expression
        if isinstance(other, ExpressionVector) and self._expression_vector.shape != other._expression_vector.shape:
            return False
        return numpy.all(self._expression_vector == other.underlying_expression)

    def __getitem__(self, item) -> Expression:
        return self._expression_vector[item]

    @property
    def underlying_expression(self) -> numpy.ndarray:
        return self._expression_vector


class ExpressionScalarOld(ExpressionOld):
    __slots__ = ('_original_expression', '_sympified_expression', '_exact_rational_lambdified')
    """A scalar mathematical expression instantiated from a string representation.
        TODO: update doc!
        TODO: write tests!
        """

    def __init__(self, ex: Union[str, Number, sympy.Expr]) -> None:
        """Create an Expression object.

        Receives the mathematical expression which shall be represented by the object as a string
        which will be parsed using py_expression_eval. For available operators, functions and
        constants see SymPy documentation

        Args:
            ex (string): The mathematical expression represented as a string
        """
        super().__init__()

        if isinstance(ex, sympy.Expr):
            self._original_expression = None
            self._sympified_expression = ex
            self._variables = get_variables(self._sympified_expression)
        elif isinstance(ex, ExpressionScalar):
            self._original_expression = ex._original_expression
            self._sympified_expression = ex._sympified_expression            
            self._variables = ex._variables
        elif isinstance(ex, (int, float)):
            self._original_expression = ex
            self._sympified_expression = sympify(ex)
            self._variables = ()
        else:
            self._original_expression = ex
            self._sympified_expression = sympify(ex)
            self._variables = get_variables(self._sympified_expression)

        self._exact_rational_lambdified = None

    def __float__(self):
        if isinstance(self._original_expression, float):
            return self._original_expression
        else:
            return super().__float__()

    @property
    def underlying_expression(self) -> sympy.Expr:
        return self._sympified_expression

    def __str__(self) -> str:
        return str(self._sympified_expression)

    def __repr__(self) -> str:
        if self._original_expression is None:
            return f"ExpressionScalar('{self._sympified_expression!r}')"
        else:
            return f"ExpressionScalar({self._original_expression!r})"

    def __format__(self, format_spec):
        if format_spec == '':
            return str(self)
        return format(float(self), format_spec)
    
    @property
    def variables(self) -> Sequence[str]:
        return self._variables

    @classmethod
    def _sympify(cls, other: Union['ExpressionScalar', Number, sympy.Expr]) -> sympy.Expr:
        return other._sympified_expression if isinstance(other, cls) else sympify(other)

    def __lt__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self._sympified_expression < self._sympify(other)
        return None if isinstance(result, sympy.Rel) else bool(result)

    def __gt__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self._sympified_expression > self._sympify(other)
        return None if isinstance(result, sympy.Rel) else bool(result)

    def __ge__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self._sympified_expression >= self._sympify(other)
        return None if isinstance(result, sympy.Rel) else bool(result)

    def __le__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> Union[bool, None]:
        result = self._sympified_expression <= self._sympify(other)
        return None if isinstance(result, sympy.Rel) else bool(result)

    def __eq__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> bool:
        """Enable comparisons with Numbers"""
        # sympy's __eq__ checks for structural equality to be consistent regarding __hash__ so we do that too
        # see https://github.com/sympy/sympy/issues/18054#issuecomment-566198899
        return self._sympified_expression == self._sympify(other)

    def __hash__(self) -> int:
        return hash(self._sympified_expression)

    def __add__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__add__(self._sympify(other)))

    def __radd__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympify(other).__radd__(self._sympified_expression))

    def __sub__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__sub__(self._sympify(other)))

    def __rsub__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__rsub__(self._sympify(other)))

    def __mul__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__mul__(self._sympify(other)))

    def __rmul__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__rmul__(self._sympify(other)))

    def __truediv__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__truediv__(self._sympify(other)))

    def __rtruediv__(self, other: Union['ExpressionScalar', Number, sympy.Expr]) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__rtruediv__(self._sympify(other)))

    def __neg__(self) -> 'ExpressionScalar':
        return self.make(self._sympified_expression.__neg__())

    def __pos__(self):
        return self.make(self._sympified_expression.__pos__())

    def _sympy_(self):
        return self._sympified_expression

    @property
    def original_expression(self) -> Union[str, Number]:
        if self._original_expression is None:
            return str(self._sympified_expression)
        else:
            return self._original_expression

    @property
    def sympified_expression(self) -> sympy.Expr:
        return self._sympified_expression

    def get_serialization_data(self) -> Union[str, float, int]:
        serialized = get_most_simple_representation(self._sympified_expression)
        if isinstance(serialized, str):
            return self.original_expression
        else:
            return serialized

    def is_nan(self) -> bool:
        return sympy.sympify('nan') == self._sympified_expression

    def _parse_evaluate_numeric_result(self,
                                       result: Union[Number, numpy.ndarray],
                                       call_arguments: Any) -> Number:
        """Overwrite super class method because we do not want to return a scalar numpy.ndarray"""
        parsed = super()._parse_evaluate_numeric_result(result, call_arguments)
        if isinstance(parsed, numpy.ndarray):
            return parsed[()]
        else:
            return parsed

    def evaluate_with_exact_rationals(self, scope: Mapping) -> Number:
        parsed_kwargs = self._parse_evaluate_numeric_arguments(scope)
        result, self._exact_rational_lambdified = evaluate_lamdified_exact_rational(self.sympified_expression,
                                                                                    self.variables,
                                                                                    parsed_kwargs,
                                                                                    self._exact_rational_lambdified)
        return self._parse_evaluate_numeric_result(result, scope)

    def evaluate_in_scope(self, scope: Mapping) -> Union[Number, numpy.ndarray]:
        if isinstance(self._original_expression, (float, int)):
            return self._original_expression
        else:
            return super().evaluate_in_scope(scope)


class ExpressionVariableMissingException(Exception):
    """An exception indicating that a variable value was not provided during expression evaluation.

    See also:
         qupulse.expressions.Expression
    """

    def __init__(self, variable: str, expression: Expression) -> None:
        super().__init__()
        self.variable = variable
        self.expression = expression

    def __str__(self) -> str:
        return "Could not evaluate <{}>: A value for variable <{}> is missing!".format(
            str(self.expression), self.variable)


class NonNumericEvaluation(Exception):
    """An exception that is raised if the result of evaluate_numeric is not a number.

    See also:
        qupulse.expressions.Expression.evaluate_numeric
    """

    def __init__(self, expression: Expression, non_numeric_result: Any, call_arguments: Dict):
        self.expression = expression
        self.non_numeric_result = non_numeric_result
        self.call_arguments = call_arguments

    def __str__(self) -> str:
        if isinstance(self.non_numeric_result, numpy.ndarray):
            dtype = self.non_numeric_result.dtype

            if dtype == numpy.dtype('O'):
                dtypes = set(map(type, self.non_numeric_result.flat))
                "The result of evaluate_numeric is an array with the types {} " \
                "which is not purely numeric".format(dtypes)
        else:
            dtype = type(self.non_numeric_result)
        return "The result of evaluate_numeric is of type {} " \
               "which is not a number".format(dtype)


ExpressionLike = TypeVar('ExpressionLike', str, Number, sympy.Expr, ExpressionScalar)

AnonymousSerializable.register(Expression)
