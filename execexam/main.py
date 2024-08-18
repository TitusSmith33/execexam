"""Run an executable examination."""

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openai
import pytest
import typer
from pytest_jsonreport.plugin import JSONReport
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from . import pytest_plugin as exec_exam_pytest_plugin

# create a Typer object to support the command-line interface
cli = typer.Typer(no_args_is_help=True)

# create a default console
console = Console()

# create the skip list for data not needed
skip = ["keywords", "setup", "teardown"]


def load_litellm():
    """Load the litellm module."""
    # note that the purpose of this function is
    # to allow the loading of the litellm module
    # to take place in a separate thread, thus
    # ensuring that the main interface is not blocked
    global litellm  # noqa: PLW0602
    global completion  # noqa: PLW0603
    from litellm import completion


def path_to_string(path_name: Path, levels: int = 4) -> str:
    """Convert the path to an elided version of the path as a string."""
    parts = path_name.parts
    if len(parts) > levels:
        return Path("<...>", *parts[-levels:]).as_posix()
    else:
        return path_name.as_posix()


def extract_details(details: Dict[Any, Any]) -> str:
    """Extract the details of a dictionary and return it as a string."""
    output = []
    # iterate through the dictionary and add each key-value pair
    for key, value in details.items():
        output.append(f"{value} {key}")
    return "Details: " + ", ".join(output)


def extract_test_run_details(details: Dict[Any, Any]) -> str:
    """Extract the details of a test run."""
    # Format of the data in the dictionary:
    # 'summary': Counter({'passed': 2, 'total': 2, 'collected': 2})
    summary_details = details["summary"]
    # convert the dictionary of summary to a string
    summary_details_str = extract_details(summary_details)
    return summary_details_str


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
            failing_test_path_str = path_to_string(failing_test_path, 4)
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
def run(
    project: Path = typer.Argument(
        ...,
        help="Project directory containing questions and tests",
    ),
    tests: Path = typer.Argument(
        ...,
        help="Test file or test directory",
    ),
    mark: str = typer.Option(
        None, help="Only run tests with the specified mark(s)"
    ),
    verbose: bool = typer.Option(False, help="Display verbose output"),
) -> None:
    """Run an executable exam."""
    litellm_thread = threading.Thread(target=load_litellm)
    litellm_thread.start()

    return_code = 0
    # add the project directory to the system path
    sys.path.append(str(project))
    # create the plugin that will collect all data
    # about the test runs and report it as a JSON object;
    # note that this approach avoids the need to write
    # a custom pytest plugin for the executable examination
    json_report_plugin = JSONReport()
    # display basic diagnostic information about command-line
    # arguments using an emoji and the rich console
    diagnostics = f"\nProject directory: {project}\n"
    diagnostics += f"Test file or test directory: {tests}\n"
    console.print()
    console.print(
        Panel(
            Text(diagnostics, overflow="fold"),
            expand=False,
            title="Parameter Information",
        )
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
    _ = extract_test_run_details(json_report_plugin.report)  # type: ignore
    # filter the test output and decide if an
    # extra newline is or is not needed
    # filtered_test_output = captured_output.getvalue()
    filtered_test_output = filter_test_output(
        "FAILED", captured_output.getvalue()
    )
    if filtered_test_output != "":
        filtered_test_output = "\n" + filtered_test_output
    console.print()
    console.print(
        Panel(
            Text(
                filtered_test_output + exec_exam_test_assertion_details,
                overflow="fold",
            ),
            expand=False,
            title="Test Overview",
        )
    )
    # --> display details about the failing tests,
    # if they exist. Note that there can be:
    # - zero failing tests
    # - one failing test
    # - multiple failing tests
    (
        failing_test_details,
        failing_test_path_dicts,
    ) = extract_failing_test_details(json_report_plugin.report)  # type: ignore
    # there was at least one failing test case
    if not is_failing_test_details_empty(failing_test_details):
        # there were test failures and thus the return code is non-zero
        # to indicate that at least one test case did not pass
        return_code = 1
        # there was a request for verbose output, so display additional
        # helpful information about the failing test cases
        if verbose:
            # display the details about the failing test cases
            console.print()
            console.print(
                Panel(
                    Text(failing_test_details, overflow="fold"),
                    expand=False,
                    title="Failing Test Details",
                )
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
                # use rich to display this source code in a formatted box
                source_code_syntax = Syntax(
                    "\n" + sanitized_output,
                    "python",
                    theme="ansi_dark",
                )
                console.print()
                console.print(
                    Panel(
                        source_code_syntax,
                        expand=False,
                        title="Failing Test Code",
                    )
                )
    # Start a thread to display the spinner
    # Display the spinner until the litellm thread finishes
    console.print()
    with console.status("[bold green] Loading ExecExam Copilot "):
        while litellm_thread.is_alive():
            time.sleep(0.1)
    litellm_thread.join()

    with console.status(
        "[bold green] Getting Feedback from ExecExam Copilot "
    ):
        test_overview = (
            filtered_test_output + exec_exam_test_assertion_details,
        )
        llm_debugging_request = (
            "I am an undergraduate student completing an examination."
            + "DO NOT make suggestions to change the test cases."
            + "DO ALWAYS make suggestions about how to improve the Python source code of the program under test."
            + "DO ALWAYS give a Python code in a Markdown fenced code block shows your suggested program."
            + "DO ALWAYS conclude saying that you making a helpful suggestion but could be wrong."
            + "Can you please suggest in a step-by-step fashion how to fix the bug in the program?"
            + f"Here is the test overview: {test_overview}"
            + f"Here are the failing test details: {failing_test_details}"
            # + f"Here is the source code for the failing test: {failing_test_code}"
        )
        response = completion(
            # model="groq/llama3-8b-8192",
            # model="anthropic/claude-3-opus-20240229",
            model="anthropic/claude-3-haiku-20240307",
            # model="anthropic/claude-instant-1.2",
            messages=[{"role": "user", "content": llm_debugging_request}],
        )
        console.print(
            Panel(
                Markdown(str(response.choices[0].message.content)),
                expand=False,
                title="ExecExam Assistant (API Key)",
                padding=1,
            )
        )
        console.print()
        # attempt with openai;
        # does not work correctly if
        # you use the standard LiteLLM
        # as done above with the extra base_url
        client = openai.OpenAI(
            api_key="anything",
            # base_url="http://0.0.0.0:4000"
            base_url="https://execexamadviser.fly.dev/",
        )
        # response = client.chat.completions.create(model="groq/llama3-8b-8192", messages = [
        response = client.chat.completions.create(
            model="anthropic/claude-3-haiku-20240307",
            messages=[
                # response = client.chat.completions.create(model="anthropic/claude-3-opus-20240229", messages = [
                {"role": "user", "content": llm_debugging_request}
            ],
        )
        console.print(
            Panel(
                Markdown(
                    "\n\n" + str(response.choices[0].message.content) + "\n\n"
                ),
                expand=False,
                title="ExecExam Assistant (Fly.io)",
                padding=1,
            )
        )

    # return the code for the overall success of the program
    # to communicate to the operating system the examination's status
    sys.exit(return_code)
