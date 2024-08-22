"""Run an executable examination."""

import io
import os
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import typer
from pytest_jsonreport.plugin import JSONReport  # type: ignore
from rich.console import Console

from . import advise, convert, display, extract
from . import pytest_plugin as exec_exam_pytest_plugin

# create a Typer object to support the command-line interface
cli = typer.Typer(no_args_is_help=True)

# create a default console
console = Console()

# create the skip list for data not needed
skip = ["keywords", "setup", "teardown"]


class Theme(str, Enum):
    """An enumeration of the themes for syntax highlighting in rich."""

    ansi_dark = "ansi_dark"
    ansi_light = "ansi_light"


def extract_test_assertion_details(test_details: Dict[Any, Any]) -> str:
    """Extract the details of a dictionary and return it as a string."""
    # create an empty list to store the output
    output = []
    # indicate that this is the first assertion
    # to be processed (it will have a "-" to start)
    first = True
    # iterate through the dictionary and add each key-value pair
    # that contains the details about the assertion
    for key, value in test_details.items():
        # this is the first assertion and thus
        # the output will start with a "-"
        if first:
            output = ["  - "]
            output.append(f"{key}: {value}\n")
            first = False
        # this is not the first assertion and thus
        # the output will start with a "  " to indent
        else:
            output.append(f"    {key}: {value}\n")
    # return each index in the output list as a string
    return "".join(output)


def extract_test_assertion_details_list(details: List[Dict[Any, Any]]) -> str:
    """Extract the details of a list of dictionaries and return it as a string."""
    output = []
    # iterate through the list of dictionaries and add each dictionary
    # to the running string that conatins test assertion details
    for current_dict in details:
        output.append(extract_test_assertion_details(current_dict))
    return "".join(output)


def extract_test_assertions_details(test_reports: List[dict[str, Any]]):
    """Extract the details of test assertions."""
    # create an empty list that will store details about
    # each test case that was execued and each of
    # the assertions that was run for that test case
    test_report_string = ""
    # iterate through the list of test reports
    # where each report is a dictionary that includes
    # the name of the test and the assertions that it ran
    for test_report in test_reports:
        # get the name of the test
        test_name = test_report["nodeid"]
        # extract only the name of the test file and the test name,
        # basically all of the content after the final slash
        display_test_name = test_name.rsplit("/", 1)[-1]
        test_report_string += f"\n{display_test_name}\n"
        # there is data about the assertions for this
        # test and thus it should be extracted and reported
        if "assertions" in test_report:
            test_report_string += extract_test_assertion_details_list(
                test_report["assertions"]
            )
    # return the string that contains all of the test assertion details
    return test_report_string


def extract_failing_test_details(
    details: dict[Any, Any],
) -> Tuple[str, List[Dict[str, Path]]]:
    """Extract the details of a failing test."""
    # extract the tests from the details
    tests = details["tests"]
    # create an empty string that starts with a newline;
    # the goal of the for loop is to incrementally build
    # of a string that contains all deteails about failing tests
    failing_details_str = "\n"
    # create an initial path for the file containing the failing test
    failing_test_paths = []
    # incrementally build up results for all of the failing tests
    for test in tests:
        if test["outcome"] == "failed":
            current_test_failing_dict = {}
            # convert the dictionary of failing details to a string
            # and add it to the failing_details_str
            failing_details = test
            # get the nodeid of the failing test
            failing_test_nodeid = failing_details["nodeid"]
            failing_details_str += f"  Name: {failing_test_nodeid}\n"
            # get the call information of the failing test
            failing_test_call = failing_details["call"]
            # get the crash information of the failing test's call
            failing_test_crash = failing_test_call["crash"]
            # extract the root of the report, which corresponds
            # to the filesystem on which the tests were run
            failing_test_path_root = details["root"]
            # extract the name of the file that contains the test
            # from the name of the individual test case itself
            failing_test_nodeid_split = failing_test_nodeid.split("::")
            # create a complete path to the file that contains the failing test file
            failing_test_path = (
                Path(failing_test_path_root) / failing_test_nodeid_split[0]
            )
            # extract the name of the function from the nodeid
            failing_test_name = failing_test_nodeid_split[-1]
            # assign the details about the failing test to the dictionary
            current_test_failing_dict["test_name"] = failing_test_name
            current_test_failing_dict["test_path"] = failing_test_path
            failing_test_paths.append(current_test_failing_dict)
            # creation additional diagnotics about the failing test
            # for further display in the console in a text-based fashion
            failing_test_path_str = convert.path_to_string(
                failing_test_path, 4
            )
            failing_test_lineno = failing_test_crash["lineno"]
            failing_test_message = failing_test_crash["message"]
            # assemble all of the failing test details into the string
            failing_details_str += f"  Path: {failing_test_path_str}\n"
            failing_details_str += f"  Line number: {failing_test_lineno}\n"
            failing_details_str += f"  Message: {failing_test_message}\n"
    # return the string that contains all of the failing test details
    return (failing_details_str, failing_test_paths)


def filter_test_output(keep_line_label: str, output: str) -> str:
    """Filter the output of the test run to keep only the lines that contain the label."""
    # create an empty string that will store the filtered output
    filtered_output = ""
    # iterate through the lines in the output
    for line in output.splitlines():
        # if the line contains the label, add it to the filtered output
        if keep_line_label in line:
            filtered_output += line + "\n"
    # return the filtered output
    return filtered_output


def is_failing_test_details_empty(details: str) -> bool:
    """Determine if the string contains a newline as a hallmark of no failing tests."""
    if details == "\n":
        return True
    return False


@cli.command()
def run(  # noqa: PLR0913
    project: Path = typer.Argument(
        ...,
        help="Project directory containing questions and tests",
    ),
    tests: Path = typer.Argument(
        ...,
        help="Test file or test directory",
    ),
    mark: str = typer.Option(None, help="Run tests with specified mark(s)"),
    fancy: bool = typer.Option(True, help="Display fancy output"),
    syntax_theme: Theme = typer.Option(
        Theme.ansi_dark, help="Syntax highlighting theme"
    ),
    verbose: bool = typer.Option(False, help="Display verbose output"),
) -> None:
    """Run an executable exam."""
    # load the litellm module in a separate thread
    litellm_thread = threading.Thread(target=advise.load_litellm)
    litellm_thread.start()
    # indicate that the program's exit code is zero
    # to show that the program completed successfully;
    # attempt to prove otherwise by running all the checks
    return_code = 0
    # add the project directory to the system path
    sys.path.append(str(project))
    # create the plugin that will collect all data
    # about the test runs and report it as a JSON object;
    # note that this approach avoids the need to write
    # a custom pytest plugin for the executable examination
    json_report_plugin = JSONReport()
    # display basic diagnostic information about command-line's arguments;
    # extract the local parmeters and then make a displayable string of them
    args = locals()
    colon_separated_diagnostics = display.make_colon_separated_string(args)
    syntax = False
    console.print()
    display.display_diagnostics(
        verbose,
        console,
        colon_separated_diagnostics,
        "Parameter Information",
        fancy,
        syntax,
        syntax_theme,
    )
    # run pytest for either:
    # - a single test file that was specified in tests
    # - a directory of test files that was specified in tests
    # note that this relies on pytest correctly discovering
    # all of the test files and running their test cases
    # redirect stdout and stderr to /dev/null
    captured_output = io.StringIO()
    sys.stdout = captured_output
    sys.stderr = captured_output
    # run pytest in a fashion that will not
    # produce any output to the console
    found_marks_str = mark
    if found_marks_str:
        pytest.main(
            [
                "-q",
                "-ra",
                "-s",
                "-p",
                "no:logging",
                "-p",
                "no:warnings",
                "--tb=no",
                "--json-report-file=none",
                "--maxfail=10",
                "-m",
                found_marks_str,
                os.path.join(tests),
            ],
            plugins=[json_report_plugin, exec_exam_pytest_plugin],
        )
    else:
        pytest.main(
            [
                "-q",
                "-ra",
                "-s",
                "-p",
                "no:logging",
                "-p",
                "no:warnings",
                "--tb=no",
                "--maxfail=10",
                "--json-report-file=none",
                os.path.join(tests),
            ],
            plugins=[json_report_plugin, exec_exam_pytest_plugin],
        )
    # restore stdout and stderr; this will allow
    # the execexam program to continue to produce
    # output in the console
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    # extract the data that was created by the internal
    # execexam pytest plugin for further diagnostic display
    execexam_report = exec_exam_pytest_plugin.reports
    # extract the details about the test assertions
    # that come from the pytest plugin that execexam uses
    exec_exam_test_assertion_details = extract_test_assertions_details(
        execexam_report
    )
    # --> display details about the test runs
    _ = extract.extract_test_run_details(json_report_plugin.report)  # type: ignore
    # filter the test output and decide if an
    # extra newline is or is not needed
    filtered_test_output = filter_test_output(
        "FAILED", captured_output.getvalue()
    )
    # add an extra newline to the filtered output
    # since there is a failing test case to display
    if filtered_test_output != "":
        filtered_test_output = "\n" + filtered_test_output
    # indicate that the material that will be displayed
    # is not source code and thus does not need syntax highlighting
    syntax = False
    display.display_content(
        console,
        filtered_test_output + exec_exam_test_assertion_details,
        "Test Trace",
        fancy,
        syntax,
        syntax_theme,
    )
    # display details about the failing tests,
    # if they exist. Note that there can be:
    # - zero failing tests
    # - one failing test
    # - multiple failing tests
    # note that details about the failing tests are
    # collected by the execexam pytest plugin and
    # there is no need for the developer of the
    # examination to collect and report this data
    (
        failing_test_details,
        failing_test_path_dicts,
    ) = extract_failing_test_details(json_report_plugin.report)  # type: ignore
    # there was at least one failing test case
    if not is_failing_test_details_empty(failing_test_details):
        # there were test failures and thus the return code is non-zero
        # to indicate that at least one test case did not pass
        return_code = 1
        # display additional helpful information about the failing
        # test cases; this is the error message that would appear
        # when standardly running the test suite with pytest
        syntax = False
        newline = True
        display.display_content(
            console,
            failing_test_details,
            "Test Failure(s)",
            fancy,
            syntax,
            syntax_theme,
            "Python",
            newline,
        )
        # display the source code for the failing test cases
        for failing_test_path_dict in failing_test_path_dicts:
            test_name = failing_test_path_dict["test_name"]
            failing_test_path = failing_test_path_dict["test_path"]
            # build the command for running symbex; this tool can
            # perform static analysis of Python source code and
            # extract the code of a function inside of a file
            command = f"symbex {test_name} -f {failing_test_path}"
            # run the symbex command and collect its output
            process = subprocess.run(
                command,
                shell=True,
                check=True,
                text=True,
                capture_output=True,
            )
            # delete an extra blank line from the end of the file
            # if there are two blank lines in a row
            sanitized_output = process.stdout.rstrip() + "\n"
            # display the source code of the failing test
            syntax = True
            newline = True
            display.display_content(
                console,
                sanitized_output,
                "Failing Test",
                fancy,
                syntax,
                syntax_theme,
                "Python",
                newline,
            )
    # display the spinner until the litellm thread finishes
    # loading the litellm module that provides the LLM-based
    # mentoring by automatically suggesting fixes for test failures
    console.print()
    with console.status("[bold green] Loading ExecExam's Coding Mentor"):
        while litellm_thread.is_alive():
            time.sleep(0.1)
    # return control to the main thread now that the
    # litellm module has been loaded in a separate thread
    litellm_thread.join()
    # advise.fix_failures(
    #     console,
    #     filtered_test_output,
    #     exec_exam_test_assertion_details,
    #     filtered_test_output + exec_exam_test_assertion_details,
    #     failing_test_details,
    #     "apiserver",
    # )
    # return the code for the overall success of the program
    # to communicate to the operating system the examination's status
    sys.exit(return_code)
