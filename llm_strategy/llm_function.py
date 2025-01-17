import dis
import functools
import inspect
import json
import types
import typing
from copy import copy, deepcopy
from dataclasses import dataclass

import pydantic.schema
import typing_extensions
from langchain.llms import BaseLLM
from langchain.output_parsers import PydanticOutputParser
from pydantic import BaseModel, create_model

T = typing.TypeVar("T")
P = typing_extensions.ParamSpec("P")


def unwrap_function(f: typing.Callable[P, T]) -> typing.Callable[P, T]:
    # is f a property?
    if isinstance(f, property):
        f = f.fget
    # is f a wrapped function?
    elif hasattr(f, "__wrapped__"):
        f = inspect.unwrap(f)
    elif inspect.ismethod(f):
        f = f.__func__
    else:
        return f

    return unwrap_function(f)


def is_not_implemented(f: typing.Callable) -> bool:
    """Check that a function only raises NotImplementedError."""
    unwrapped_f = unwrap_function(f)

    if not hasattr(unwrapped_f, "__code__"):
        raise ValueError(f"Cannot check whether {f} is implemented. Where is __code__?")

    # Inspect the opcodes
    code = unwrapped_f.__code__
    # Get the opcodes
    opcodes = list(dis.get_instructions(code))
    # Check that it only uses the following opcodes:
    # - RESUME
    # - LOAD_GLOBAL
    # - PRECALL
    # - CALL
    # - RAISE_VARARGS
    valid_opcodes = {
        "RESUME",
        "LOAD_GLOBAL",
        "PRECALL",
        "CALL",
        "RAISE_VARARGS",
    }
    # We allow at most a function of length len(valid_opcodes)
    if len(opcodes) > len(valid_opcodes):
        return False
    for opcode in opcodes:
        if opcode.opname not in valid_opcodes:
            return False
        # Check that the function only raises NotImplementedError
        if opcode.opname == "LOAD_GLOBAL" and opcode.argval != "NotImplementedError":
            return False
        if opcode.opname == "RAISE_VARARGS" and opcode.argval != 1:
            return False
        valid_opcodes.remove(opcode.opname)
    # Check that the function raises a NotImplementedError at the end.
    if opcodes[-1].opname != "RAISE_VARARGS":
        return False
    return True


class TyperWrapper(str):
    """
    A wrapper around a type that can be used to create a Pydantic model.

    This is used to support @classmethods.
    """

    @classmethod
    def __get_validators__(cls) -> typing.Iterator[typing.Callable]:
        # one or more validators may be yielded which will be called in the
        # order to validate the input, each validator will receive as an input
        # the value returned from the previous validator
        yield cls.validate

    @classmethod
    def validate(cls, v: type) -> str:
        if not isinstance(v, type):
            raise TypeError("type required")
        return v.__qualname__


@dataclass
class LLMFunctionSpec:
    signature: inspect.Signature
    docstring: str
    input_model: typing.Type[BaseModel]
    output_model: typing.Type[BaseModel]

    def get_call_schema(self) -> dict:
        schema = pydantic.schema.schema([self.input_model, self.output_model])
        definitions: dict = deepcopy(schema["definitions"])
        # remove title and type from each sub dict in the definitions
        for value in definitions.values():
            value.pop("title")
            value.pop("type")

        return definitions

    @staticmethod
    def from_function(f: typing.Callable[P, T]) -> "LLMFunctionSpec":
        """Create an LLMFunctionSpec from a function."""

        # get clean docstring of
        docstring = inspect.getdoc(f)
        if docstring is None:
            raise ValueError("The function must have a docstring.")
        # get the type of the first argument
        signature = inspect.signature(f, eval_str=True)
        # get all parameters
        parameters = signature.parameters
        # create a pydantic model from the parameters
        parameter_dict = {}
        for parameter_name, parameter in parameters.items():
            # every parameter must be annotated or have a default value
            annotation = parameter.annotation
            if annotation is type:
                annotation = TyperWrapper

            if annotation is inspect.Parameter.empty:
                # check if the parameter has a default value
                if parameter.default is inspect.Parameter.empty:
                    raise ValueError(f"The parameter {parameter_name} must be annotated or have a default value.")
                parameter_dict[parameter_name] = parameter.default
            elif parameter.default is inspect.Parameter.empty:
                parameter_dict[parameter_name] = (annotation, ...)
            else:
                parameter_dict[parameter_name] = (
                    annotation,
                    parameter.default,
                )
        # create the model
        input_model = create_model("Inputs", **parameter_dict)
        input_model.update_forward_refs()
        # get the return type
        return_type = signature.return_annotation
        if return_type is inspect.Signature.empty:
            raise ValueError("The function must have a return type.")
        # create the output model
        # the return type can be a type annotation or an Annotated type with annotation being a FieldInfo
        if typing.get_origin(return_type) is typing.Annotated:
            return_info = typing.get_args(return_type)
        else:
            return_info = (return_type, ...)
        output_model = create_model("Outputs", return_value=return_info)
        output_model.update_forward_refs()

        return LLMFunctionSpec(
            docstring=docstring,
            signature=signature,
            input_model=input_model,
            output_model=output_model,
        )


@dataclass
class LLMFunction(typing.Generic[P, T], typing.Callable[P, T]):  # type: ignore
    """
    A callable that can be called with a chat model.
    """

    language_model: BaseLLM

    def __get__(self, instance: object, owner: type | None = None) -> typing.Callable:
        """Support instance methods."""
        if instance is None:
            return self

        # Bind self to instance as MethodType
        return types.MethodType(self, instance)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        """Call the function."""
        spec = LLMFunctionSpec.from_function(self)

        # bind the inputs to the signature
        bound_arguments = spec.signature.bind(*args, **kwargs)
        # get the arguments
        arguments = bound_arguments.arguments
        inputs = spec.input_model(**arguments)

        # get the input adn output schema as JSON dict
        schema = spec.get_call_schema()

        # create the prompt
        prompt = (
            "{docstring}\n"
            "\n"
            "The input is formatted as a JSON interface of Inputs that conforms to the JSON schema below, "
            "and the output should be formatted as a JSON instance of Outputs that conforms to the JSON "
            "schema below.\n"
            "\n"
            'As an example, for the schema {{"properties": {{"foo": {{"title": "Foo", "description": "a list of '
            'strings", "type": "array", "items": {{"type": "string"}}}}}}, "required": ["foo"]}}}} the object {{'
            '"foo": ["bar", "baz"]}} is a well-formatted instance of the schema. The object {{"properties": {{"foo": '
            '["bar", "baz"]}}}} is not well-formatted.\n'
            "\n"
            "Here is the schema:\n"
            "```\n"
            "{schema}\n"
            "```\n"
            "\n"
            "Now output the results for the following inputs:\n"
            "```\n"
            "{inputs}\n"
            "```\n"
        ).format(docstring=spec.docstring, schema=json.dumps(schema), inputs=inputs.json())

        # get the chat model
        language_model = self.language_model
        if language_model is None:
            raise ValueError("The chat model must be set.")

        # get the output
        output = language_model(prompt)

        # parse the output
        parser = PydanticOutputParser(pydantic_object=spec.output_model)
        parsed_output = parser.parse(output)

        return parsed_output.return_value  # type: ignore


def can_wrap_function_in_llm(f: typing.Callable[P, T]) -> bool:
    """
    Return True if f can be wrapped in an LLMCall.
    """
    unwrapped = unwrap_function(f)
    if not callable(unwrapped):
        return False
    if inspect.isgeneratorfunction(unwrapped):
        return False
    if inspect.iscoroutinefunction(unwrapped):
        return False
    if not inspect.isfunction(unwrapped) and not inspect.ismethod(unwrapped):
        return False
    return is_not_implemented(unwrapped)


def llm_function(language_model: BaseLLM) -> typing.Callable[[typing.Callable[P, T]], LLMFunction[P, T]]:
    """
    Decorator to wrap a function with a chat model.

    f is a function to a dataclass or Pydantic model.

    The docstring of the function provides instructions for the model.
    """

    def decorator(f: typing.Callable[P, T]) -> LLMFunction[P, T]:
        if isinstance(f, classmethod):
            return classmethod(decorator(f.__func__))
        elif isinstance(f, staticmethod):
            return staticmethod(decorator(f.__func__))
        elif isinstance(f, property):
            return property(decorator(f.fget), doc=f.__doc__)
        elif isinstance(f, types.MethodType):
            return types.MethodType(decorator(f.__func__), f.__self__)
        elif hasattr(f, "__wrapped__"):
            return decorator(f.__wrapped__)
        elif isinstance(f, LLMFunction):
            f = copy(f)
            f.language_model = language_model
            return f
        elif not callable(f):
            raise ValueError(f"Cannot decorate {f} with llm_strategy.")

        if not is_not_implemented(f):
            raise ValueError("The function must not be implemented.")

        specific_llm_function: LLMFunction = functools.wraps(f)(LLMFunction(language_model=language_model))
        return specific_llm_function

    return decorator
