"""Commands for interacting with a remote MAAS."""

__all__ = [
    "colorized",
    "Command",
    "CommandError",
    "OriginCommand",
    "OriginTableCommand",
    "PROFILE_DEFAULT",
    "PROFILE_NAMES",
    "TableCommand",
]

from abc import (
    ABCMeta,
    abstractmethod,
)
import argparse
import code
from importlib import import_module
import sys
from typing import (
    Optional,
    Sequence,
    Tuple,
)

import argcomplete
import colorclass

from . import tabular
from .. import (
    bones,
    utils,
    viscera,
)
from ..utils import profiles


def colorized(text):
    if sys.stdout.isatty():
        # Don't return value_colors; returning the Color instance allows
        # terminaltables to correctly calculate alignment and padding.
        return colorclass.Color(text)
    else:
        return colorclass.Color(text).value_no_colors


def get_profile_names_and_default() -> Tuple[
        Sequence[str], Optional[profiles.Profile]]:
    """Return the list of profile names and the default profile object.

    The list of names is sorted.
    """
    with profiles.ProfileManager.open() as config:
        return sorted(config), config.default


# Get profile names and the default profile now to avoid repetition when
# defining arguments (e.g. default and choices). Doing this as module-import
# time is imperfect but good enough for now.
PROFILE_NAMES, PROFILE_DEFAULT = get_profile_names_and_default()


class ArgumentParser(argparse.ArgumentParser):
    """Specialisation of argparse's parser with better support for subparsers.

    Specifically, the one-shot `add_subparsers` call is disabled, replaced by
    a lazily evaluated `subparsers` property.
    """

    def add_subparsers(self):
        raise NotImplementedError(
            "add_subparsers has been disabled")

    @property
    def subparsers(self):
        """Obtain the subparser's object."""
        try:
            return self.__subparsers
        except AttributeError:
            parent = super(ArgumentParser, self)
            self.__subparsers = parent.add_subparsers(title="sub-commands")
            self.__subparsers.metavar = "COMMAND"
            return self.__subparsers

    def __getitem__(self, name):
        """Return the named subparser."""
        return self.subparsers.choices[name]

    def error(self, message):
        """Make the default error messages more helpful

        Override default ArgumentParser error method to print the help menu
        generated by ArgumentParser instead of just printing out a list of
        valid arguments.
        """
        self.exit(2, colorized("{autored}Error:{/autored} ") + message + "\n")


class CommandError(Exception):
    """A command has failed during execution."""


class Command(metaclass=ABCMeta):
    """A base class for composing commands.

    This adheres to the expectations of `register`.
    """

    def __init__(self, parser):
        super(Command, self).__init__()
        self.parser = parser

    @abstractmethod
    def __call__(self, options):
        """Execute this command."""

    @classmethod
    def name(cls):
        """Return the preferred name as which this command will be known."""
        name = cls.__name__.replace("_", "-").lower()
        name = name[4:] if name.startswith("cmd-") else name
        return name

    @classmethod
    def register(cls, parser, name=None):
        """Register this command as a sub-parser of `parser`.

        :type parser: An instance of `ArgumentParser`.
        """
        help_title, help_body = utils.parse_docstring(cls)
        command_parser = parser.subparsers.add_parser(
            cls.name() if name is None else name, help=help_title,
            description=help_title, epilog=help_body)
        command_parser.set_defaults(execute=cls(command_parser))


class TableCommand(Command):

    def __init__(self, parser):
        super(TableCommand, self).__init__(parser)
        if sys.stdout.isatty():
            default_target = tabular.RenderTarget.pretty
        else:
            default_target = tabular.RenderTarget.plain
        parser.add_argument(
            "--output-format", type=tabular.RenderTarget,
            choices=tabular.RenderTarget, default=default_target, help=(
                "Output tabular data as a formatted table (pretty), a "
                "formatted table using only ASCII for borders (plain), or "
                "one of several dump formats. Default: %(default)s."
            ),
        )


class OriginCommandBase(Command):

    def __init__(self, parser):
        super(OriginCommandBase, self).__init__(parser)
        parser.add_argument(
            "--profile-name", metavar="NAME", choices=PROFILE_NAMES,
            required=(PROFILE_DEFAULT is None), help=(
                "The name of the remote MAAS instance to use. Use "
                "`list-profiles` to obtain a list of valid profiles." +
                ("" if PROFILE_DEFAULT is None else " [default: %(default)s]")
            ))
        if PROFILE_DEFAULT is not None:
            parser.set_defaults(profile_name=PROFILE_DEFAULT.name)


class OriginCommand(OriginCommandBase):

    def __call__(self, options):
        session = bones.SessionAPI.fromProfileName(options.profile_name)
        origin = viscera.Origin(session)
        return self.execute(origin, options)

    def execute(self, options, origin):
        raise NotImplementedError(
            "Implement execute() in subclasses.")


class OriginTableCommand(OriginCommandBase, TableCommand):

    def __call__(self, options):
        session = bones.SessionAPI.fromProfileName(options.profile_name)
        origin = viscera.Origin(session)
        return self.execute(origin, options, target=options.output_format)

    def execute(self, options, origin, *, target):
        raise NotImplementedError(
            "Implement execute() in subclasses.")


class cmd_shell(Command):
    """Start an interactive shell with some convenient local variables.

    If IPython is available it will be used, otherwise the familiar Python
    REPL will be started. If a script is piped in, it is read in its entirety
    then executed with the same namespace as the interactive shell.
    """

    def __init__(self, parser):
        super(cmd_shell, self).__init__(parser)
        parser.add_argument(
            "--profile-name", metavar="NAME", choices=PROFILE_NAMES,
            required=False, help=(
                "The name of the remote MAAS instance to use. Use "
                "`list-profiles` to obtain a list of valid profiles." +
                ("" if PROFILE_DEFAULT is None else " [default: %(default)s]")
            ))
        if PROFILE_DEFAULT is None:
            parser.set_defaults(profile_name=None)
        else:
            parser.set_defaults(profile_name=PROFILE_DEFAULT.name)

    def __call__(self, options):
        """Execute this command."""

        namespace = {}  # The namespace that code will run in.
        variables = {}  # Descriptions of the namespace variables.

        # If a profile has been selected, set up a `bones` session and a
        # `viscera` origin in the default namespace.
        if options.profile_name is not None:
            session = bones.SessionAPI.fromProfileName(options.profile_name)
            namespace["session"] = session
            variables["session"] = (
                "A `bones` session, configured for %s."
                % options.profile_name)
            origin = viscera.Origin(session)
            namespace["origin"] = origin
            variables["origin"] = (
                "A `viscera` origin, configured for %s."
                % options.profile_name)

        # Display some introductory text if this is fully interactive.
        if sys.stdin.isatty() and sys.stdout.isatty():
            banner = ["{automagenta}Welcome to the MAAS shell.{/automagenta}"]
            if len(variables) > 0:
                banner += ["", "Predefined variables:", ""]
                banner += [
                    "{autoyellow}%10s{/autoyellow}: %s" % variable
                    for variable in sorted(variables.items())
                ]
            for line in banner:
                print(colorized(line))

        # Start IPython or the REPL if stdin is from a terminal, otherwise
        # slurp everything and exec it within `namespace`.
        if sys.stdin.isatty():
            try:
                import IPython
            except ImportError:
                code.InteractiveConsole(namespace).interact(" ")
            else:
                IPython.start_ipython(
                    argv=[], display_banner=False, user_ns=namespace)
        else:
            source = sys.stdin.read()
            exec(source, namespace, namespace)


def prepare_parser(argv):
    """Create and populate an argument parser."""
    parser = ArgumentParser(
        description="Interact with a remote MAAS server.", prog=argv[0],
        epilog="http://maas.ubuntu.com/")

    # Top-level commands.
    cmd_shell.register(parser)

    # Create sub-parsers for various command groups. These are all verbs.
    parser.subparsers.add_parser(
        "acquire", help="Acquire nodes or other resources.")
    parser.subparsers.add_parser(
        "launch", help="Launch nodes or other resources.")
    parser.subparsers.add_parser(
        "release", help="Release nodes or other resources.")
    parser.subparsers.add_parser(
        "list", help="List nodes, files, tags, and other resources.")

    # Register sub-commands.
    submodules = "profiles", "files", "nodes", "tags", "users"
    for submodule in submodules:
        module = import_module("." + submodule, __name__)
        module.register(parser)

    # Register global options.
    parser.add_argument(
        '--debug', action='store_true', default=False,
        help=argparse.SUPPRESS)

    return parser


def post_mortem(traceback):
    """Work with an exception in a post-mortem debugger.

    Try to use `ipdb` first, falling back to `pdb`.
    """
    try:
        from ipdb import post_mortem
    except ImportError:
        from pdb import post_mortem

    post_mortem(traceback)


def main(argv=sys.argv):
    parser = prepare_parser(argv)
    argcomplete.autocomplete(parser, exclude=("-h", "--help"))

    options = None
    try:
        options = parser.parse_args(argv[1:])
        try:
            execute = options.execute
        except AttributeError:
            parser.error("No arguments given.")
        else:
            execute(options)
    except KeyboardInterrupt:
        raise SystemExit(1)
    except Exception as error:
        if options is None or options.debug:
            *_, exc_traceback = sys.exc_info()
            post_mortem(exc_traceback)
            raise
        else:
            # Note: this will call sys.exit() when finished.
            parser.error("%s" % error)