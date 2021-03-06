# Copyright (c) 2014 Matthew Rocklin
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   a. Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#   b. Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#   c. Neither the name of multipledispatch nor the names of its contributors
#      may be used to endorse or promote products derived from this software
#      without specific prior written permission.
#
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
# DAMAGE.
#
# --------------------------------------------------------------------------------------
# MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
#
# Copyright (c) 2014-2020 Megvii Inc. All rights reserved.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#
#  This file has been modified by Megvii ("Megvii Modifications").
#  All Megvii Modifications are Copyright (C) 2014-2020 Megvii Inc. All rights reserved.
# --------------------------------------------------------------------------------------

import copy
import inspect
import itertools as itl
from warnings import warn

from ..._imperative_rt.dispatcher import Dispatcher as CDispatcher
from .conflict import AmbiguityWarning, ambiguities, ordering, super_signature
from .utils import expand_tuples, parse_union
from .variadic import Variadic, isvariadic


def ambiguity_warn(dispatcher, ambiguities):
    """ Raise warning when ambiguity is detected

    Parameters
    ----------
    dispatcher : Dispatcher
        The dispatcher on which the ambiguity was detected
    ambiguities : set
        Set of type signature pairs that are ambiguous within this dispatcher

    See Also:
        Dispatcher.add
        warning_text
    """
    warn(warning_text(dispatcher.name, ambiguities), AmbiguityWarning)


def variadic_signature_matches_iter(types, full_signature):
    """
    Check if a set of input types matches a variadic signature.

    Notes
    -----
    The algorithm is as follows:

    Initialize the current signature to the first in the sequence

    For each type in `types`:
        If the current signature is variadic
            If the type matches the signature
                yield True
            Else
                Try to get the next signature
                If no signatures are left we can't possibly have a match
                    so yield False
        Else
            yield True if the type matches the current signature
            Get the next signature
    """
    sigiter = iter(full_signature)
    sig = next(sigiter)
    for typ in types:
        matches = issubclass(typ, sig)
        yield matches
        if not isvariadic(sig):
            # we're not matching a variadic argument, so move to the next
            # element in the signature
            sig = next(sigiter)
    else:
        try:
            sig = next(sigiter)
        except StopIteration:
            assert isvariadic(sig)
            yield True
        else:
            # We have signature items left over, so all of our arguments
            # haven't matched
            yield False


def variadic_signature_matches(types, full_signature):
    # No arguments always matches a variadic signature
    assert full_signature
    return all(variadic_signature_matches_iter(types, full_signature))


def get_func_signature(function):
    sig = inspect.signature(function)
    types = []
    for p in sig.parameters.values():
        ann = p.annotation
        ann = parse_union(ann) or ann
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            types.append(ann)
        if p.kind == inspect.Parameter.VAR_POSITIONAL:
            types.append([ann])
    return tuple(types)


class Frame:
    __slots__ = "args", "types", "mro", "mro_offset"


class Dispatcher(CDispatcher):
    """ Dispatch methods based on type signature

    Use ``dispatch`` to add implementations

    Examples
    --------

    >>> from multipledispatch import dispatch
    >>> @dispatch(int)
    ... def f(x):
    ...     return x + 1

    >>> @dispatch(float)
    ... def f(x):
    ...     return x - 1

    >>> f(3)
    4
    >>> f(3.0)
    2.0
    """

    __slots__ = "__name__", "name", "funcs", "_ordering", "doc"

    def __init__(self, name, doc=None):
        self.name = self.__name__ = name
        self.funcs = {}
        self.doc = doc

    def register(self, *types, **kwargs):
        """ register dispatcher with new implementation

        >>> f = Dispatcher('f')
        >>> @f.register(int)
        ... def inc(x):
        ...     return x + 1

        >>> @f.register(float)
        ... def dec(x):
        ...     return x - 1

        >>> @f.register(list)
        ... @f.register(tuple)
        ... def reverse(x):
        ...     return x[::-1]

        >>> f(1)
        2

        >>> f(1.0)
        0.0

        >>> f([1, 2, 3])
        [3, 2, 1]
        """

        def _df(func):
            self.add(types, func, **kwargs)
            return func

        return _df

    def add(self, signature, func):
        """ Add new types/method pair to dispatcher

        >>> D = Dispatcher('add')
        >>> D.add((int, int), lambda x, y: x + y)
        >>> D.add((float, float), lambda x, y: x + y)

        >>> D(1, 2)
        3
        >>> D(1, 2.0)
        Traceback (most recent call last):
        ...
        NotImplementedError: Could not find signature for add: <int, float>

        When ``add`` detects a warning it calls the ``on_ambiguity`` callback
        with a dispatcher/itself, and a set of ambiguous type signature pairs
        as inputs.  See ``ambiguity_warn`` for an example.
        """
        # Handle annotations
        if not signature:
            signature = get_func_signature(func)

        # Handle union types
        if any(isinstance(typ, tuple) for typ in signature):
            for typs in expand_tuples(signature):
                self.add(typs, func)
            return

        new_signature = []

        for index, typ in enumerate(signature, start=1):
            if not isinstance(typ, (type, list)):
                str_sig = ", ".join(
                    c.__name__ if isinstance(c, type) else str(c) for c in signature
                )
                raise TypeError(
                    "Tried to dispatch on non-type: %s\n"
                    "In signature: <%s>\n"
                    "In function: %s" % (typ, str_sig, self.name)
                )

            # handle variadic signatures
            if isinstance(typ, list):
                if index != len(signature):
                    raise TypeError("Variadic signature must be the last element")

                if len(typ) != 1:
                    raise TypeError(
                        "Variadic signature must contain exactly one element. "
                        "To use a variadic union type place the desired types "
                        "inside of a tuple, e.g., [(int, str)]"
                    )
                new_signature.append(Variadic[typ[0]])
            else:
                new_signature.append(typ)

        l = self.funcs.setdefault(tuple(new_signature), [])
        for i in l:
            if i is func:
                raise ValueError("already registered")
        l.append(func)
        self.enable(func)
        self.clear_cache()

        try:
            del self._ordering
        except AttributeError:
            pass

    @property
    def ordering(self):
        try:
            return self._ordering
        except AttributeError:
            return self.reorder()

    def reorder(self, on_ambiguity=ambiguity_warn):
        self._ordering = od = ordering(self.funcs)
        amb = ambiguities(self.funcs)
        if amb:
            on_ambiguity(self, amb)
        return od

    def __str__(self):
        return "<dispatched %s>" % self.name

    __repr__ = __str__

    def dispatch(self, *types):
        """
        Deterimine appropriate implementation for this type signature

        This method is internal.  Users should call this object as a function.
        Implementation resolution occurs within the ``__call__`` method.

        >>> from multipledispatch import dispatch
        >>> @dispatch(int)
        ... def inc(x):
        ...     return x + 1

        >>> implementation = inc.dispatch(int)
        >>> implementation(3)
        4

        >>> print(inc.dispatch(float))
        None

        See Also:
          ``multipledispatch.conflict`` - module to determine resolution order
        """

        if types in self.funcs:
            return self.funcs[types][-1]

        for f in self.dispatch_iter(*types):
            return f

    def dispatch_iter(self, *types):

        n = len(types)
        for signature in self.ordering:
            if (
                len(signature) == n
                and all(map(issubclass, types, signature))
                or len(signature)
                and isvariadic(signature[-1])
                and variadic_signature_matches(types, signature)
            ):
                yield from self.funcs[signature][::-1]

    def __getstate__(self):
        return {"name": self.name, "funcs": self.funcs}

    def __setstate__(self, d):
        self.name = d["name"]
        self.funcs = d["funcs"]
        self._ordering = ordering(self.funcs)
        self._cache = dict()

    @property
    def __doc__(self):
        docs = ["Multiply dispatched method: %s" % self.name]

        if self.doc:
            docs.append(self.doc)

        other = []
        for sig in self.ordering[::-1]:
            funcs = self.funcs[sig]
            s = "Inputs: <%s>\n" % str_signature(sig)
            sep = "-" * len(s) + "\n"
            for i, func in enumerate(funcs):
                s += sep
                if len(funcs) > 1:
                    s += "[Handler %d]\n\n" % (i + 1)
                    if i:
                        s += "\n\n"
                if func.__doc__:
                    s += func.__doc__.strip()
                else:
                    s += repr(func) + "\n"
            docs.append(s)

        return "\n\n".join(docs)

    def _help(self, *args):
        return self.dispatch(*map(type, args)).__doc__

    def help(self, *args, **kwargs):
        """ Print docstring for the function corresponding to inputs """
        print(self._help(*args))

    def _source(self, *args):
        func = self.dispatch(*map(type, args))
        if not func:
            raise TypeError("No function found")
        return source(func)

    def source(self, *args, **kwargs):
        """ Print source code for the function corresponding to inputs """
        print(self._source(*args))


def source(func):
    s = "File: %s\n\n" % inspect.getsourcefile(func)
    s = s + inspect.getsource(func)
    return s


class MethodDispatcher(Dispatcher):
    """ Dispatch methods based on type signature

    See Also:
        Dispatcher
    """

    __slots__ = ("obj", "cls")

    @classmethod
    def get_func_params(cls, func):
        if hasattr(inspect, "signature"):
            sig = inspect.signature(func)
            return itl.islice(sig.parameters.values(), 1, None)

    def __get__(self, instance, owner):
        self.obj = instance
        self.cls = owner
        return self

    def __call__(self, *args, **kwargs):
        types = tuple([type(arg) for arg in args])
        func = self.dispatch(*types)
        if not func:
            raise NotImplementedError(
                "Could not find signature for %s: <%s>"
                % (self.name, str_signature(types))
            )
        return func(self.obj, *args, **kwargs)


def str_signature(sig):
    """ String representation of type signature

    >>> str_signature((int, float))
    'int, float'
    """
    return ", ".join(cls.__name__ for cls in sig)


def warning_text(name, amb):
    """ The text for ambiguity warnings """
    text = "\nAmbiguities exist in dispatched function %s\n\n" % (name)
    text += "The following signatures may result in ambiguous behavior:\n"
    for pair in amb:
        text += "\t" + ", ".join("[" + str_signature(s) + "]" for s in pair) + "\n"
    text += "\n\nConsider making the following additions:\n\n"
    text += "\n\n".join(
        [
            "@dispatch(" + str_signature(super_signature(s)) + ")\ndef %s(...)" % name
            for s in amb
        ]
    )
    return text
