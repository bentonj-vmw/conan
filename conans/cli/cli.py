import importlib
import os
import pkgutil
import signal
import sys
import textwrap
from collections import defaultdict
from difflib import get_close_matches
from inspect import getmembers

from conans import __version__ as client_version
from conans.cli.api.conan_api import ConanAPIV2, ConanAPI
from conans.cli.command import ConanSubCommand
from conans.cli.exit_codes import SUCCESS, ERROR_MIGRATION, ERROR_GENERAL, USER_CTRL_C, \
    ERROR_SIGTERM, USER_CTRL_BREAK, ERROR_INVALID_CONFIGURATION, ERROR_INVALID_SYSTEM_REQUIREMENTS
from conans.cli.output import ConanOutput, cli_out_write, Color
from conans.client.command import Command
from conans.client.conan_api import ConanAPIV1
from conans.errors import ConanInvalidSystemRequirements
from conans.errors import ConanException, ConanInvalidConfiguration, ConanMigrationError
from conans.util.files import exception_message_safe
from conans.util.log import logger


CLI_V1_COMMANDS = [
    'get', 'upload',
    'test', 'source', 'build', 'editable', 'imports',
    'download', 'inspect'
]


class Cli:
    """A single command of the conan application, with all the first level commands. Manages the
    parsing of parameters and delegates functionality to the conan python api. It can also show the
    help of the tool.
    """

    def __init__(self, conan_api):
        assert isinstance(conan_api, (ConanAPIV1, ConanAPIV2)), \
            "Expected 'Conan' type, got '{}'".format(type(conan_api))
        self._conan_api = conan_api
        self._groups = defaultdict(list)
        self._commands = {}
        conan_commands_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands")
        for module in pkgutil.iter_modules([conan_commands_path]):
            module_name = module[1]
            self._add_command("conans.cli.commands.{}".format(module_name), module_name)
        user_commands_path = os.path.join(self._conan_api.cache_folder, "commands")
        sys.path.append(user_commands_path)
        for module in pkgutil.iter_modules([user_commands_path]):
            module_name = module[1]
            if module_name.startswith("cmd_"):
                self._add_command(module_name, module_name.replace("cmd_", ""))

    def _add_command(self, import_path, method_name):
        try:
            command_wrapper = getattr(importlib.import_module(import_path), method_name)
            if command_wrapper.doc:
                self._commands[command_wrapper.name] = command_wrapper
                self._groups[command_wrapper.group].append(command_wrapper.name)
            for name, value in getmembers(importlib.import_module(import_path)):
                if isinstance(value, ConanSubCommand):
                    if name.startswith("{}_".format(method_name)):
                        command_wrapper.add_subcommand(value)
                    else:
                        raise ConanException("The name for the subcommand method should "
                                             "begin with the main command name + '_'. "
                                             "i.e. {}_<subcommand_name>".format(method_name))
        except AttributeError:
            raise ConanException("There is no {} method defined in {}".format(method_name,
                                                                              import_path))

    def _print_similar(self, command):
        """ Looks for similar commands and prints them if found.
        """
        output = ConanOutput()
        matches = get_close_matches(
            word=command, possibilities=self._commands.keys(), n=5, cutoff=0.75)

        if len(matches) == 0:
            return

        if len(matches) > 1:
            output.info("The most similar commands are")
        else:
            output.info("The most similar command is")

        for match in matches:
            output.info("    %s" % match)

        output.writeln("")

    def _output_help_cli(self):
        """
        Prints a summary of all commands.
        """
        max_len = max((len(c) for c in self._commands)) + 1
        line_format = '{{: <{}}}'.format(max_len)

        for group_name, comm_names in self._groups.items():
            cli_out_write(group_name, Color.BRIGHT_MAGENTA)
            for name in comm_names:
                # future-proof way to ensure tabular formatting
                cli_out_write(line_format.format(name), Color.GREEN, endline="")

                # Help will be all the lines up to the first empty one
                docstring_lines = self._commands[name].doc.split('\n')
                start = False
                data = []
                for line in docstring_lines:
                    line = line.strip()
                    if not line:
                        if start:
                            break
                        start = True
                        continue
                    data.append(line)

                txt = textwrap.fill(' '.join(data), 80, subsequent_indent=" " * (max_len + 2))
                cli_out_write(txt)

        cli_out_write("")
        cli_out_write('Conan commands. Type "conan help <command>" for help', Color.BRIGHT_YELLOW)

    def run(self, *args):
        """ Entry point for executing commands, dispatcher to class
        methods
        """
        output = ConanOutput()
        try:
            try:
                command_argument = args[0][0]
            except IndexError:  # No parameters
                self._output_help_cli()
                return SUCCESS
            try:
                command = self._commands[command_argument]
            except KeyError as exc:
                if command_argument in ["-v", "--version"]:
                    cli_out_write("Conan version %s" % client_version, fg=Color.BRIGHT_GREEN)
                    return SUCCESS

                if command_argument in ["-h", "--help"]:
                    self._output_help_cli()
                    return SUCCESS

                output.info("'%s' is not a Conan command. See 'conan --help'." % command_argument)
                output.info("")
                self._print_similar(command_argument)
                raise ConanException("Unknown command %s" % str(exc))
        except ConanException as exc:
            output.error(exc)
            return ERROR_GENERAL

        try:
            command.run(self._conan_api, self._commands[command_argument].parser, args[0][1:])
            exit_error = SUCCESS
        except SystemExit as exc:
            if exc.code != 0:
                logger.error(exc)
                output.error("Exiting with code: %d" % exc.code)
            exit_error = exc.code
        except ConanInvalidConfiguration as exc:
            exit_error = ERROR_INVALID_CONFIGURATION
            output.error(exc)
        except ConanInvalidSystemRequirements as exc:
            exit_error = ERROR_INVALID_SYSTEM_REQUIREMENTS
            output.error(exc)
        except ConanException as exc:
            exit_error = ERROR_GENERAL
            output.error(exc)
        except Exception as exc:
            import traceback
            print(traceback.format_exc())
            exit_error = ERROR_GENERAL
            msg = exception_message_safe(exc)
            output.error(msg)

        return exit_error


def main(args):
    """ main entry point of the conan application, using a Command to
    parse parameters

    Exit codes for conan command:

        0: Success (done)
        1: General ConanException error (done)
        2: Migration error
        3: Ctrl+C
        4: Ctrl+Break
        5: SIGTERM
        6: Invalid configuration (done)
    """

    # Temporary hack to call the legacy command system if the command is not yet implemented in V2
    command_argument = args[0] if args else None
    is_v1_command = command_argument in CLI_V1_COMMANDS

    try:
        conan_api = ConanAPIV1() if is_v1_command else ConanAPI()
    except ConanMigrationError:  # Error migrating
        sys.exit(ERROR_MIGRATION)
    except ConanException as e:
        sys.stderr.write("Error in Conan initialization: {}".format(e))
        sys.exit(ERROR_GENERAL)

    def ctrl_c_handler(_, __):
        print('You pressed Ctrl+C!')
        sys.exit(USER_CTRL_C)

    def sigterm_handler(_, __):
        print('Received SIGTERM!')
        sys.exit(ERROR_SIGTERM)

    def ctrl_break_handler(_, __):
        print('You pressed Ctrl+Break!')
        sys.exit(USER_CTRL_BREAK)

    signal.signal(signal.SIGINT, ctrl_c_handler)
    signal.signal(signal.SIGTERM, sigterm_handler)

    if sys.platform == 'win32':
        signal.signal(signal.SIGBREAK, ctrl_break_handler)

    if is_v1_command:
        command = Command(conan_api)
        exit_error = command.run(args)
    else:
        cli = Cli(conan_api)
        exit_error = cli.run(args)

    sys.exit(exit_error)
