# ---- Setup ----
import requests
import json
import pytest
from dataclasses import dataclass, field
from dotenv import load_dotenv
import os

target_remote_url = "http://0.0.0.0:8502"

# The list of chatbots to try, in order of preference. The first chatbot which the server accepts will be used for testing.
preferred_chatbots = [
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    # Only fall back to smaller/older models if necessary
    "gpt-5-mini",
    "gpt-4.1",
    "o3",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-oss:20b",
]
# The actual selected chatbot, filled by the select_chatbot fixture.
selected_chatbot: str | None = None


load_dotenv()

base_url: str | None = None
headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def set_globals(request: pytest.FixtureRequest):
    global base_url
    global headers
    user_token = request.config.getoption("--freva-user-token", None)
    # If the token is set, we are in remote mode.
    if user_token:
        base_url = target_remote_url
        headers["x-freva-user-token"] = "Bearer " + user_token
    else:
        base_url = "http://localhost:8502/api/chatbot"
        # The user token still needs to be set to something.
        headers["x-freva-user-token"] = (
            "Bearer TOKEN_MOCK_AUTH_ACCEPS_ANYTHING_FOR_LOCAL_MODE"
        )
        headers["x-freva-vault-url"] = (
            "http://127.0.0.1:5001"  # TODO: this doesn't work with docker/podman?
        )
        headers["x-freva-rest-url"] = "http://localhost:5001"
    # Also overwrite base_url with target_url, if provided
    target_url = request.config.getoption("--target-url", None)
    if target_url and isinstance(target_url, str):
        base_url = target_url


auth_key = os.getenv("AUTH_KEY", "no_auth_key")
global_user_id = "testing"
auth_string = "&auth_key=" + auth_key + "&user_id=" + global_user_id  # Only for testing
# In Version 1.6.1, the freva_config also needs to be set to a specific path. We won't be using this for now.
auth_string = auth_string + "&freva_config=" + "Cargo.toml"  # Dummy value


# ======================================
# ---- Helper Functions and Classes ----
# ======================================


def get_request(url, stream=False):
    return requests.get(base_url + url + auth_string, stream=stream, headers=headers)


def do_request_and_maybe_fail(url):
    response = get_request(url)
    print(response.text)
    r_json = response.json()
    # If the response is a dict with a key "detail" that starts with "Token check failed", abort early.
    if (
        response.status_code == 401
        and "detail" in r_json
        and isinstance(r_json["detail"], str)
        and r_json["detail"].startswith("Token check failed")
    ):
        pytest.exit(
            "Token check failed! This means that the provided user token is invalid, or not valid anymore. Please provide a valid token with the --freva-user-token option to run the tests in remote mode."
        )
    return r_json


def get_avail_chatbots():
    return do_request_and_maybe_fail("/availablechatbots?")


def get_user_threads(num_threads=None, page=0) -> tuple[list, int]:
    # The python backend doesn't support "num_threads=" as an option, so we need to handle the case where num_threads is None.
    if num_threads is None:
        num_threads = 10
    return do_request_and_maybe_fail(
        f"/getuserthreads?num_threads={num_threads}&page={page}"
    )


def set_thread_topic(thread_id, new_topic):
    return do_request_and_maybe_fail(
        f"/setthreadtopic?thread_id={thread_id}&topic={new_topic}"  # It's "topic", not "new_topic" now
    )


def search_database(query: str):
    # The python backend seems to always expect a num_threads parameter, so we will just set it to 10.
    return do_request_and_maybe_fail(f"/searchthreads?query={query}&num_threads=10")


def delete_thread(thread_id: str):
    return do_request_and_maybe_fail(f"/deletethread?thread_id={thread_id}")


def fork_thread(thread_id: str, fork_from_index: int) -> str:
    response = do_request_and_maybe_fail(
        f"/editthread?source_thread_id={thread_id}&fork_from_index={fork_from_index}"
    )
    print(response)
    return response["new_thread_id"]


@dataclass
class StreamResult:
    chatbot: str | None
    thread_id: str
    raw_response: list = field(default_factory=list)
    json_response: list = field(default_factory=list)
    code_variants: list = field(default_factory=list)
    codeoutput_variants: list = field(default_factory=list)
    assistant_variants: list = field(default_factory=list)
    image_variants: list = field(default_factory=list)
    server_hint_variants: list = field(default_factory=list)
    parsed_list: list = field(
        default_factory=list
    )  # Full list of variants, with combined fragments.

    def has_error_variants(self):
        return any(["error" in i["variant"].lower() for i in self.json_response])


def parse_response_variants(
    json_response, raw_response: list | None = None, thread_id: str | None = None
) -> StreamResult:
    code_variants = []
    assistant_variants = []
    codeoutput_variants = []
    image_variants = []
    server_hint_variants = []

    # DEBUG
    print("Debug: Extracting variants from json_response: ", json_response)
    # The stream can stream multiple Assistant or Code fragments one after the other, in order to get good UX, but that means that multiple fragments that form a single variant can be streamed one after the other.
    # So, for convenience, we'll combine consecutive fragments that form a single variant into a single variant, if that variant is Assistant or Code.

    full_list = []  # Full list of variants, with combined fragments.

    running_code = (
        None  # None or tuple of (code, code_id) (which is the content of the fragment)
    )
    running_assistant = None  # None or string (which is the content of the fragment)
    for fragment in json_response:
        variant = fragment["variant"]
        content = fragment["content"]

        if variant != "Code" and running_code:
            code_variants.append(running_code)
            full_list.append({"variant": "Code", "content": running_code})
            running_code = None
        if variant != "Assistant" and running_assistant:
            assistant_variants.append(running_assistant)
            full_list.append({"variant": "Assistant", "content": running_assistant})
            running_assistant = None

        if variant == "Code":
            # Python doesn't encode the ID as a part of the content (the second object in the tuple),
            # but rather as a separate field in the JSON object. So we need to check for that and combine it with the code content if it exists.
            actual_content = (
                content[0]
                if isinstance(content, list) and len(content) > 0
                else content
            )  # if it's a list, assume Rust and take the first element, else take the content directly, which is how the Python backend does it.
            actual_id = (
                content[1]
                if isinstance(content, list) and len(content) > 1
                else fragment.get("id", None)
            )  # The code_id can be either in the content (Rust) or in a separate field (Python), so we check both places.
            if running_code:
                running_code = (
                    running_code[0] + actual_content,
                    running_code[1],
                )
            else:
                running_code = (actual_content, actual_id)
        elif variant == "Assistant":
            if running_assistant:
                running_assistant = running_assistant + content
            else:
                running_assistant = content
        elif variant == "CodeOutput":
            # The same divide on how the ID is encoded also exists for the CodeOutput
            actual_content = (
                content[0]
                if isinstance(content, list) and len(content) > 0
                else content
            )
            codeoutput_variants.append(actual_content)
            full_list.append({"variant": variant, "content": actual_content})
        elif variant == "Image":
            image_variants.append(content)
            full_list.append({"variant": variant, "content": content})
        elif variant == "ServerHint":
            server_hint_variants.append(content)
            full_list.append({"variant": variant, "content": content})
        elif (
            variant == "User"
            or variant == "OpenAIError"
            or variant == "CodeError"
            or variant == "StreamEnd"
        ):
            full_list.append({"variant": variant, "content": content})
        else:
            print(f"Unknown variant {variant} found in response!")

    # If there is still a running code or assistant, add it to the list.
    # But this shouldn't really happen, every stream should be ended with a StreamEnd variant.
    if running_code:
        code_variants.append(running_code)
        full_list.append(("Code", running_code))
    if running_assistant:
        assistant_variants.append(running_assistant)
        full_list.append(("Assistant", running_assistant))

    # The python backend puts the payload in directly, so the dict doesn't need to be parsed.
    # If it's a string, decode it, else use it directly.
    # I actually don't understand why this code worked before; the Rust backend should actually not always send the thread_id.
    # Now that it isn't being sent every time, this breaks. I'll just set the thread_id from outside if it's already known.
    if thread_id:
        thread_id = thread_id
    elif server_hint_variants and isinstance(server_hint_variants[0], str):
        thread_id = json.loads(server_hint_variants[0])["thread_id"]
    elif server_hint_variants and isinstance(server_hint_variants[0], dict):
        thread_id = server_hint_variants[0]["thread_id"]
    if not thread_id:
        # self.thread_id = None
        raise ValueError(
            "No thread_id found in server hints! Server hints were: "
            + str(server_hint_variants)
        )  # We should really not try to continue if we don't have a thread_id

    print(
        "Debug: thread_id: " + (thread_id or "None")
    )  # Alway print the thread_id for debugging, so that when a test fails, we know which thread_id to look at.
    return StreamResult(
        chatbot=None,
        thread_id=thread_id,
        raw_response=raw_response,
        json_response=json_response,
        code_variants=code_variants,
        codeoutput_variants=codeoutput_variants,
        assistant_variants=assistant_variants,
        image_variants=image_variants,
        server_hint_variants=server_hint_variants,
        parsed_list=full_list,
    )


def generate_full_response(
    user_input, chatbot=None, thread_id=None, user_id=None, fork_from_index=None
) -> StreamResult:
    inner_url = "/streamresponse?input=" + user_input
    if chatbot:
        inner_url = inner_url + "&chatbot=" + chatbot
    if fork_from_index is not None:
        # The new backend has a separate endpoint for forking threads.
        # First, fork the thread, grab the new thread_id, then call the streamresponse endpoint with the new thread_id.
        if thread_id is None:
            raise ValueError(
                "thread_id must be provided if fork_from_index is provided"
            )
        thread_id = fork_thread(thread_id, fork_from_index)
        # Just continue with the new thread_id, which will be used in the streamresponse call below.
    if thread_id:
        inner_url = inner_url + "&thread_id=" + thread_id

    print("Debug: Generating full response with URL: " + inner_url)

    # The response is streamed, but we will consume it here and store it
    response = get_request(inner_url, stream=True)

    # Because the python request library is highly unreliable when it comes to streaming, we will manually assemble the response packet by packet here.
    raw_response = []
    reconstructed_packets = []
    buffer = ""
    for delta in response:
        # print(delta) # Debugging
        data = delta.decode("utf-8")
        buffer += data
        raw_response.append(data)

        # Each packet is a valid JSON object, so we try to parse the buffer until we get a successful parse.
        # Each packet must end at a }, so we will only consider the buffer from the start to each }.

        packet_found = True
        while packet_found:
            packet_found = False
            closing_brace_locations = [
                i for i in range(len(buffer)) if buffer[i] == "}"
            ]

            for closing_brace_location in closing_brace_locations:
                # Try to parse the buffer up to the closing brace location
                try:
                    packet = json.loads(buffer[: closing_brace_location + 1])
                    reconstructed_packets.append(packet)
                    buffer = buffer[closing_brace_location + 1 :]
                    packet_found = True
                except json.JSONDecodeError:
                    # If we get a JSONDecodeError, we will just ignore it and continue
                    pass

        # All packets that we could parse are now in reconstructed_packets, and the buffer contains the rest of the data.
    result = parse_response_variants(
        json_response=reconstructed_packets,
        raw_response=raw_response,
        thread_id=thread_id,
    )

    # Print the response for debugging, so that when a test fails, we know what the response was.
    print("Debug: Assistant variants: ")
    print(result.assistant_variants)
    print("Debug: Code variants: ")
    print(result.code_variants)
    print("Debug: CodeOutput variants: ")
    print(result.codeoutput_variants)
    # print("Debug: full json_response: ") # Disabled, too noisy
    # print(result.json_response)
    assert not result.has_error_variants(), "Error variants found in response!"
    return result


def get_thread_by_id(thread_id):
    reponse = get_request("/getthread?thread_id=" + thread_id)
    print(reponse.text)
    return reponse.json()


# ===========================
# ---- Testing functions ----
# ===========================


def test_is_up():
    get_request("/ping")
    get_request("/docs")


def print_help():
    response = get_request("/help")  # Same as /ping
    print(response.text)


def print_docs():
    response = get_request("/docs")
    print(response.text)


@pytest.fixture(autouse=True)
def test_available_chatbots():
    response = get_avail_chatbots()
    # Instead of manually checking which chatbots are available, compare the response to the list of preferred chatbots and select the first one that is available.
    for chatbot in preferred_chatbots:
        if chatbot in response:
            global selected_chatbot
            selected_chatbot = chatbot
            print(f"Selected chatbot: {selected_chatbot}")
            break
    assert selected_chatbot is not None, (
        "No preferred chatbot found in response! Response was: " + ", ".join(response)
    )


def get_hello_world_thread_id() -> str:
    response = generate_full_response(
        "Please use the code_interpreter tool to run the following code exactly and only once: \"print('Hello\\nWorld\\n!', flush=True)\".",
        chatbot=selected_chatbot,
    )
    # Just make sure the code output contains "Hello World !"
    assert any("Hello\nWorld\n!" in i for i in response.codeoutput_variants)
    assert response.thread_id, "No thread_id found"
    # Now return the thread_id for further testing
    return response.thread_id


def test_hello_world():
    """Does the printing of Hello World work?"""
    thread_id = get_hello_world_thread_id()
    # Now use the thread_id to test the getthread endpoint
    hw_thread = get_thread_by_id(thread_id)  # Type: list of variants.
    temp = parse_response_variants(json_response=hw_thread)
    delete_thread(
        thread_id
    )  # Clean up after the test, to not clutter the database with test threads.
    # But do it as soon as possible to make sure that failing asserts don't prevent cleanup.
    # Some later tests can't guarantee that because of the multi-turn conversations, they work on best-effort.

    assert temp.thread_id == thread_id  # Just make sure the thread_id is correct
    assert any(
        "Hello\nWorld\n!" in i for i in temp.codeoutput_variants
    )  # Make sure the code output contains "Hello World !"


def test_sine_wave(display=False):
    """Can the code_interpreter tool handle matplotlib and output an image?"""  # Base functionality test
    response = generate_full_response(
        "This is a test regarding your capabilities of using the code_interpreter tool and whether it supports matplotlib. Please use the code_interpreter tool to run the following code: \"import numpy as np\nimport matplotlib.pyplot as plt\nt = np.linspace(-2 * np.pi, 2 * np.pi, 100)\nsine_wave = np.sin(t)\nplt.figure(figsize=(10, 5))\nplt.plot(t, sine_wave, label='Sine Wave')\nplt.title('Sine Wave from -2π to 2π')\nplt.xlabel('Angle (radians)')\nplt.ylabel('Sine value')\nplt.axhline(0, color='black', linewidth=0.5, linestyle='--')\nplt.axvline(0, color='black', linewidth=0.5, linestyle='--')\nplt.grid()\nplt.legend()\nplt.show()\".",
        chatbot=selected_chatbot,
    )
    # We want to make sure we have generated code, code output and an image. But we want to print the assistant response if it fails.
    print(response.assistant_variants)
    delete_thread(response.thread_id)
    assert response.code_variants
    assert response.codeoutput_variants
    assert response.image_variants

    # Only possible in a notebook
    # if display: # For manual testing, ipytest won't display the image
    #     from IPython.display import display, Image
    #     from base64 import b64decode
    #     for image in response.image_variants:
    #         display(Image(data=b64decode(image), format='png'))


def test_persistent_thread_storage():
    """Does the backend remember the content of a thread?"""  # Base functionality test
    response = generate_full_response(
        "Please add 2+2 in the code_interpreter tool.", chatbot=selected_chatbot
    )
    # Now follow up with another request to the same thread_id, to test whether the storage is persistent
    response2 = generate_full_response(
        "Now please multiply the result by 3.",
        chatbot=selected_chatbot,
        thread_id=response.thread_id,
    )
    delete_thread(response.thread_id)
    # The code output should now contain 12
    assert any("12" in i for i in response2.codeoutput_variants)


def test_persistant_state_storage():
    """Can the backend refer to the same variable in different tool calls?"""  # Since Version 1.6.3
    # Here, we want to test whether the value of a variable is stored between tool calls (not requests)
    response = generate_full_response(
        'Please assign the value 42 to the variable x in the code_interpreter tool. After that, call the tool with the code "print(x, flush=True)", without assigning x again. It\'s a test for the presistance of data.',
        chatbot=selected_chatbot,
    )
    delete_thread(response.thread_id)
    # The code output should now contain 42
    assert any("42" in i for i in response.codeoutput_variants)
    # Also make sure there are actually two code variants
    assert len(response.code_variants) == 2


def test_persistant_xarray_storage():
    """Can the backend refer to the same xarray in different tool calls?"""  # Since Version 1.6.5
    response = generate_full_response(
        'Please generate a simple xarray dataset in the code_interpreter tool and print out the content. After that, call the tool with the code "print(ds, flush=True)", without generating the dataset again. It\'s a test for the presistance of data, specifically whether xarray Datasets also work.',
        chatbot=selected_chatbot,
    )
    delete_thread(response.thread_id)
    # The code output should now contain the content of the xarray dataset
    assert any(
        ("xarray.Dataset" in i or "xarray.DataArray" in i)
        for i in response.codeoutput_variants
    )
    # Also make sure there are actually two code variants
    assert (
        len(response.code_variants) >= 2
    )  # The LLM sometimes messes up and needs to try again...


# TODO: is this test translatable to the python backend?
# def test_models_available():
#     ''' Can the backend use the common models qwen2.5:3b, o4-mini and gpt-5-nano? ''' # Since Version 1.7.1, 1.10.1 and 1.10.2 respectively.
#     qwen_response = generate_full_response("This is a test request for your basic functionality. Please respond with (200 Ok) and exit. Don't use the code interpreter, just say it.", chatbot="qwen2.5:3b")
#     # The assistant output should now contain "200 Ok"
#     assert any("200 ok" in i.lower() for i in qwen_response.assistant_variants)

#     o4_mini_response = generate_full_response("This is a test request for your basic functionality. Please respond with (200 Ok) and exit. Don't use the code interpreter, just say it.", chatbot="o4-mini")
#     # The assistant output should now contain "200 Ok"
#     assert any("200 ok" in i.lower() for i in o4_mini_response.assistant_variants)

#     gpt_5_nano_response = generate_full_response("This is a test request for your basic functionality. Please respond with (200 Ok) and exit. Don't use the code interpreter, just say it.", chatbot="gpt-5-nano")
#     # The assistant output should now contain "200 Ok"
#     assert any("200 ok" in i.lower() for i in gpt_5_nano_response.assistant_variants)


# def test_qwen_code_interpreter():
#     ''' Can the backend get a code response from Qwen? ''' # Since Version 1.7.1
#     response = generate_full_response("Please use the code_interpreter tool to run `print(2938429834 * 234987234)`. Make sure to adhere to the JSON format!", chatbot="qwen2.5:3b")
#     # The code output should now contain the result of the multiplication
#     assert any("690493498994739156" in i for i in response.codeoutput_variants)


def test_heartbeat():
    """Can the backend send a heartbeat while a long calculation is running?"""  # Since Version 1.8.1
    response = generate_full_response(
        'Please use the code_interpreter tool to run the following code: "import time\ntime.sleep(7)".',
        chatbot=selected_chatbot,
    )
    delete_thread(response.thread_id)
    # There should now, in total be at least two ServerHint Variants
    # (The Rust backend incorrectly sent two heartbeats at the start of the stream, but the Python backend only sends one, so it's 2 instead of 3)
    assert len(response.server_hint_variants) >= 2
    # The second Serverhint (first is thread_id) should be JSON containing "memory", "total_memory", "cpu_last_minute", "process_cpu" and "process_memory"
    # The python backend doesn't do double encoding, so the content is already a dict
    # first_hearbeat = json.loads(response.server_hint_variants[1])
    first_hearbeat = response.server_hint_variants[1]
    assert "memory" in first_hearbeat
    assert "total_memory" in first_hearbeat
    assert "cpu_last_minute" in first_hearbeat
    assert "process_cpu" in first_hearbeat
    assert "process_memory" in first_hearbeat


# TODO: implement 1.8.3 feature of stopping a tool call! (and the 1.8.9 feature that derives from it)


# def test_syntax_hinting():
#     """Can the backend provide extended hints on syntax errors?"""  # Since Version 1.8.4
#     response = generate_full_response(
#         "Please use the code_interpreter tool to run the following code: \"print('Hello World'\". This is a test for the improved syntax error reporting. If a hint containing the syntax error is returned, the test is successful.",
#         chatbot=selected_chatbot,
#     )
#     # We can now check the Code Output for the string "Hint: the error occured on line", as well as "SyntaxError"
#     assert any(
#         "Hint: the error occured on line" in i for i in response.codeoutput_variants
#     )
#     assert any("SyntaxError" in i for i in response.codeoutput_variants)
# The old backend used to print a small hint to point at the erroring line as well as the surrounding lines.
# This was mainly because the old backend injected code before the code of the chatbot, so the line numbers in the error message didn't match.
# Since the new backend's MCP server does proper Python/Jupyter error messages, which the chatbots are directly familiar with,
# this is almost definitely not necessary anymore.
# #TODO: We can maybe reintroduce this test/feature later if we want to, but for now, I don't see the need for it


def test_regression_variable_storage():
    """Does the backend correctly handle the edge case of variable storage?"""  # Since Version 1.8.9
    input = "This is a test on a corner case of the code_interpreter tool: variables don't seem to be stored if the code errors before the last line.\
To test this. Please run the following code: \"x = 42\nraise Exception('This is a test exception')\nprint('Padding for last-line-logic')\","
    response = generate_full_response(input, chatbot=selected_chatbot)
    # The code output should now contain the exception message
    assert any(["This is a test exception" in i for i in response.codeoutput_variants])

    # Now make sure the variable x is still stored
    response2 = generate_full_response(
        "Please demonstrate the fact that the code interpreter does not persist variables after exceptions by printing x without reassigning it.",
        chatbot=selected_chatbot,
        thread_id=response.thread_id,
    )
    delete_thread(response.thread_id)
    # The code output should now contain 42
    assert any(["42" in i for i in response2.codeoutput_variants])
    assert any(["42" in i for i in response2.codeoutput_variants])


def test_third_request():
    """Can the backend store information the user gave over multiple requests?"""  # Since Version 1.8.14
    # The test is for a regression that happened when the backend moved to mongodb from storing threads on disk.
    # Basically, I forgot to append the existing thread, so the conten was just overwritten.
    # This lead to the chatbot not being able to recall what the user wrote in their first request, once they do a third request, hence the name.

    # response1 = generate_full_response("Please remember the following information: \"I am a software engineer and I like to play chess\".", chatbot="gpt-4o-mini")
    # This doesn't work well because it technically does work, but is not in the style of what frevaGPT was designed to work with.
    response1 = generate_full_response(
        "Hi! I'm Sebastian from the DRKZ. Who are you?", chatbot=selected_chatbot
    )
    # The assistant should now remember the users name.
    response2 = generate_full_response(
        "Nice to meet you! What do you think about chess?",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
    )  # Just some filler. I'm not good at small talk.

    response3 = generate_full_response(
        "What was my name again?",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
    )

    delete_thread(response1.thread_id)
    assert any("Sebastian" in i for i in response3.assistant_variants)


should_test_mongo = True


def test_get_user_threads():
    """Can the Frontend request the threads of a user?"""  # Since Version 1.9.0
    # Version 1.9.0 introduced the ability to request the threads of a user.
    # This requires MongoDB to be turned on, so this switch can be turned off to disable this feature.
    if should_test_mongo:
        response = get_user_threads()
        # Since Version 1.11.0, this also includes the total number of threads.
        assert (
            isinstance(response, list)
            or (isinstance(response, tuple))
            and len(response) == 2
        )
        threads, num_threads = response
        assert isinstance(num_threads, int)
        assert num_threads >= len(threads)
        # The response should be a list of threads, each with a thread_id and a chatbot name
        assert isinstance(threads, list)
        assert all(isinstance(i, dict) for i in threads)
        assert all("thread_id" in i for i in threads)
        assert all("user_id" in i for i in threads)
        assert all("date" in i for i in threads)
        assert all("topic" in i for i in threads)
        assert all("content" in i for i in threads)
        for i in threads:
            assert isinstance(i["thread_id"], str)
            assert isinstance(i["user_id"], str)
            assert isinstance(i["date"], str)
            assert isinstance(i["topic"], str)
            assert isinstance(i["content"], list)
            inner_content = i["content"]
            # The content is a list of Stream Variants. Each must have a variant and a content
            assert all(isinstance(j, dict) for j in inner_content)
            assert all("variant" in j for j in inner_content)
            assert all("content" in j for j in inner_content)


def test_update_topic():
    """Can the frontend update the topic of a past thread?"""  # Since Version 1.11.1
    if not should_test_mongo:
        return
    # So there is the get_user_threads endpoint which returns the past few threads of a user.
    # We'll grab the latest thread, check the topic, set it to another value, and check whether that worked.
    response = get_user_threads()
    print(response)
    threads = response[0]
    latest_thread = threads[0] if threads else None
    assert latest_thread is not None, "No threads found"

    old_topic = latest_thread["topic"]
    new_topic = (
        old_topic + " - Updated"
    )  # To make sure it can never accidentally match the old topic

    result = set_thread_topic(latest_thread["thread_id"], new_topic)
    print(result)

    updated_thread = get_user_threads()[
        0
    ][
        0
    ]  # First thread (second top level item is the total number of threads of that user.)
    assert updated_thread["topic"] == new_topic, "Failed to update thread topic"


# def test_get_user_threads_paginated():
#     """Can the frontend request a specific page of threads?"""  # Since Version 1.11.1
#     if not should_test_mongo:
#         return

#     # We'll run a standard request against get_user threads with n=2 and then another with page = 1 and check that no thread is in both results.
#     response_page_0 = get_user_threads(num_threads=2)  # page = 0 is implied
#     response_page_1 = get_user_threads(num_threads=2, page=1)
#     thread_ids_0 = set([i["thread_id"] for i in response_page_0[0]])
#     thread_ids_1 = set([i["thread_id"] for i in response_page_1[0]])

#     # Set intersection should be empty.
#     assert not thread_ids_0 & thread_ids_1, "Threads should be different between pages"
# No, the backend cannot yet do pagination.
# TODO: implement pagination in the backend and then reenable this test.


def test_query_database():
    """Can the frontend query the database?"""  # Since Version 1.11.1
    if not should_test_mongo:
        return

    response = search_database("test")
    print(response)
    # We can't test this in a useful way, so we'll just check that everything is in the right format.
    assert response is not None
    assert isinstance(response, list)
    assert (
        len(response) == 2
    )  # First is the content, second is the total number of results
    assert response[1] >= len(response[0])
    response = response[0]  # Check the content
    assert isinstance(response, list)
    for i in response:
        assert "thread_id" in i
        assert "user_id" in i
        assert "date" in i
        assert "topic" in i
        assert "content" in i


def test_query_database_prefix():
    """Can the frontend query the database while specifying the variant where the query string occured?"""  # Since Version 1.11.1
    # The prefix is a bit underdocumented, but if we specify "ai:sorry", it should specifically search for threads where the assistant said sorry.
    # This is again quite hard to test for, so we instead to two request, where the second replaces the colon with a space and check whether that changes the result
    if not should_test_mongo:
        return
    response_ai = search_database(
        "user :variable x"
    )  # This definitely occurs due to the test_persistant_variable_storage test.
    print(response_ai)
    response_space = search_database(
        "user variable x"
    )  # This probably doesn't occur, so the result should be different.
    print(response_space)
    assert response_ai != response_space, "Responses should be different"


# def test_use_rw_dir():
#     """Does the LLM understand how it can use the rw directory?"""  # Since Version 1.9.0
#     # The rw directory is a directory that the LLM can use to store and load files for the user.
#     # This is a test to see if the LLM can use it correctly.
#     # It should also infer that if the user wants to save a file, it should use the rw directory.
#     # TODO: remove the hint for user_id and thread_id and make sure it still works
#     response = generate_full_response(
#         "This is a test. Please generate a plot of a sine wave from -2π to 2π and save it as a PNG file. Remember to save it in the proper location with user_id and thread_id.",
#         chatbot=selected_chatbot,
#     )
#     # print(response)
#     # Afer this, it should have generated a file in the rw directory.
#     # Specifically, at "rw_dir/testing/{thread_id}/????.png"
#     # So we check whether that directory exists and contains a file.
#     thread_id = response.thread_id
#     rw_dir = f"rw_dir/testing/{thread_id}"
#     print(f"Debug: rw_dir: {rw_dir}")  # Debugging
#     assert os.path.exists(rw_dir), f"RW directory {rw_dir} does not exist!"

#     # Make sure there is at least one file in the directory
#     files = os.listdir(rw_dir)
#     print(f"Debug: Files in rw_dir: {files}")  # Debugging
#     assert len(files) > 0, f"RW directory {rw_dir} is empty!"
# TODO: since the remote testing doesn't write locally, this test doesn't really work.
# Maybe find a way to make it work again?


def test_user_vision():
    """Can the LLM see the output that it generated?"""  # Since Version 1.10.0

    # The LLM should be able to see the image that the code it wrote generated.
    response = generate_full_response(
        "You should have access to vision capabilities. To test them, please generate two random numbers, x and y, between -1 and 1, without printing them, and plot a big red X at the position (x, y) in a 100x100 pixel image. Then please tell me where the X is located in the image, whether it's up, down, left, right or in the center. Do not print the coordinates, save the image somehwere or write any code except for the plotting of the X! Look at the generated image instead.",
        chatbot=selected_chatbot,
    )

    # print(response) # Debug
    delete_thread(response.thread_id)

    # The response should contain an image and the assistant should not be confused about the location of the X.
    assert response.image_variants, "No image variants found in response!"
    negatives = [
        "i don't know",
        "i can't see",
        "i can't tell",
        "i'm not sure",
        "i don't understand",
        "unfortunately",
        "i don't",
        "i cannot",
        "i can't",
    ]
    assert not any(
        neg in i.lower() for i in response.assistant_variants for neg in negatives
    ), (
        "Assistant was confused about the location of the X! It either refused or couldn't see it."
    )

    # Also make sure that the assistant didn't print out the coordinates.
    # For that, test the code output for numbers, that is 0.[0-9]+
    assert not any("0." in i for i in response.codeoutput_variants), (
        "Assistant printed out the coordinates of the X! It should only describe the location in words, not numbers."
    )

    # Lastly make sure it actually generated an answer
    valid_answers = ["up", "down", "left", "right", "center"]
    # assert any(i.lower() in valid_answers for i in response.assistant_variants), "Assistant did not return a valid answer about the location of the X! It should have returned one of: " + ", ".join(valid_answers) + ". Instead, it returned: " + ", ".join(response.assistant_variants)
    assert any(
        [v in ("".join(response.assistant_variants)).lower() for v in valid_answers]
    ), (
        "Assistant did not return a valid answer about the location of the X! It should have returned one of: "
        + ", ".join(valid_answers)
        + ". Instead, it returned: "
        + ", ".join(response.assistant_variants)
    )


def test_non_alphanumeric_user_id():
    """Can the backend handle non-alphanumeric user IDs?"""  # Since Version 1.10.1
    # The backend should be able to handle non-alphanumeric user IDs, such as emails.
    # This is a regression test for a bug that was introduced in Version 1.10.0, where the backend would fail to create the rw directory if the user ID contained non-alphanumeric characters
    # and then failed fully.

    try:
        # First, we need to set the user ID to a non-alphanumeric value.
        global global_user_id
        global_user_id = "example@web.de"  # This is a valid email address, but contains non-alphanumeric characters.
        # Now we can run the test
        response = generate_full_response(
            "This is simple test. Please just return 'OK' and exit.",
            chatbot=selected_chatbot,
        )
        delete_thread(response.thread_id)
        # The response should contain "OK"
        assert any("OK" in i for i in response.assistant_variants), (
            "Assistant did not return 'OK'! Instead, it returned: "
            + ", ".join(response.assistant_variants)
        )
    finally:
        # Reset the user ID to the default value, so that the other tests can run without issues.
        global_user_id = "testing"


def test_edit_input():
    """Can the backend handle edits to the user input?"""  # Since Version 1.10.3
    # For the EVE demonstration, we want the capability to edit a past user input.
    # This is a test for whether the backend can handle that.
    response1 = generate_full_response(
        "Hi, I'm Sebastian! Who are you?", chatbot=selected_chatbot
    )  # Give it my name, to later test whether it remembers it.
    # The response content doesn't matter yet.
    response2 = generate_full_response(
        "Nice to meet you! I've heard about the DKRZ and am a student of the university of Hamburg. Where is the DKRZ located?",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
    )

    # Now we can edit the second request to run the test.
    # The LLM should have remembered my name, but not the fact that I am a student of the university of Hamburg.
    response3 = generate_full_response(
        "Thank you. This is a test for the functionality to edit existing requests. What do you know about me?",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
        fork_from_index=3,
    )
    # The response should contain my name, but not the fact that I am a student of the university of Hamburg.
    assert "sebastian" in "".join(response3.assistant_variants).lower(), (
        "Assistant did not remember my name! Instead, it returned: "
        + ", ".join(response3.assistant_variants)
    )
    assert "student" not in "".join(response3.assistant_variants).lower(), (
        "Assistant remembered the fact that I am a student of the university of Hamburg, but it should not have"
    )
    assert (
        "university of hamburg" not in "".join(response3.assistant_variants).lower()
    ), (
        "Assistant remembered the fact that I am a student of the university of Hamburg, but it should not have"
    )

    # Additionally, the new response needs to have a different thread_id, so we can test that.
    assert response3.thread_id != response1.thread_id, (
        "The thread_id of the edited response is the same as the original response! It should be different, so that the frontend can handle it correctly."
    )

    # And that new thread_id should also be stored in the database, so we can test that.
    # We know what it should contain.
    retrived_thread = get_thread_by_id(response3.thread_id)

    # Note that the thread_id of response1 and response3 differ, as per the assert in the middle of this test.
    delete_thread(response1.thread_id)
    delete_thread(response3.thread_id)

    assert retrived_thread, (
        "The thread with the edited response was not found in the database! It should have been stored, so that the frontend can handle it correctly."
    )
    print("retrived_thread: ", retrived_thread)

    # There may be some other variants in the thread still, so we only keep those that are User or Assistant.
    retrived_thread = [
        i for i in retrived_thread if i["variant"] in ["User", "Assistant"]
    ]
    # Now we can check the length of the thread.
    # There should be 4 variants in total, the first and third should be User, and the second and fourth should be Assistant.
    # The first variant is the User input, the second variant is the Assistant response, the third variant is the User edit, and the fourth variant is the Assistant response to the edit.

    assert len(retrived_thread) == 4, (
        "The thread with the edited response should have 4 variants, but it has "
        + str(len(retrived_thread))
        + "."
    )
    # The first and third variants should be User, and the second and fourth variants should be Assistant.
    assert retrived_thread[0]["variant"] == retrived_thread[2]["variant"] == "User", (
        "The first and third variants of the thread with the edited response should be User, but they are not."
    )
    assert (
        retrived_thread[1]["variant"] == retrived_thread[3]["variant"] == "Assistant"
    ), (
        "The second and fourth variants of the thread with the edited response should be Assistant, but they are not."
    )

    # Recently, the protocol for the edit functionality changed slightly, so that the previous variants, before the edit point are also sent.
    # This makes it easier for the frontend to display the thread correctly. But now the test case above has changed, so we'll add some additional checks.
    # NOTE: the user variant is NOT sent from the backend, so the entire thread EXPECT the edited user input will be sent.
    # The Serverhint variant will be where the edit is indicated.

    # We'll not use the thread from the database, but rather the one from the response, to make sure that the response is correct.
    # Note: the new backend doesn't send the already sent variants again since it does it with a seperate call (tbh, it's cleaner)
    # That means that the sent variants only include a single Assistant variant.
    retrived_thread = [
        i for i in response3.parsed_list if i["variant"] in ["User", "Assistant"]
    ]

    print("retrived_thread of response: ", retrived_thread)

    # There should be 1 variant in total, only the assistant.

    assert len(retrived_thread) == 1, (
        "The edited response should have 1 variants, but it has "
        + str(len(retrived_thread))
        + ". Instead, it has the following variants: "
        + str(retrived_thread)
    )

    assert retrived_thread[0]["variant"] == "Assistant", (
        "The second variant of the thread with the edited response should be Assistant, but it is "
        + retrived_thread[0]["variant"]
        + "."
    )

    # # Additionally, the content of the user variant is known and should be checked.
    # assert retrived_thread[0]["content"] == "Hi, I'm Sebastian! Who are you?", (
    #     'The content of the first variant of the thread with the edited response is not correct! It should be "Hi, I\'m Sebastian! Who are you?", but it is "'
    #     + retrived_thread[0]["content"]
    #     + '".'
    # )
    # Instead of containing the entire content; this system stores only the added context in the new thread.
    # It then stores the thread_id of the parent separately. The parent thread_id is not currently readable from outside, so we can't test it.
    # TODO: maybe test the parent thread_id being correct once it's available.


def test_edit_input_with_code():
    """If the frontend sends an edit request with code, does the backend handle it correctly?"""  # Since Version 1.10.3
    # Because the backend has python pickles to store the variables, it should be able to handle edits with code.

    # We'll do the same setup as in previous tests for persistant; so one request to set a variable, a test request to check the variable, and then an edit request to change the variable.
    response1 = generate_full_response(
        'Please assign the value 42 to the variable x in the code_interpreter tool. After that, call the tool with the code "print(x, flush=True)", without assigning x again. It\'s a test for the presistance of data.',
        chatbot=selected_chatbot,
    )
    # The code output should now contain 42
    assert any("42" in i for i in response1.codeoutput_variants)
    assert len(response1.code_variants) == 2, (
        "The code variants should contain two variants, one for the assignment of x and one for the print statement."
    )

    response2 = generate_full_response(
        "Now print the value of x without assigning it again.",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
    )
    # The code output should now contain 42
    assert any("42" in i for i in response2.codeoutput_variants)
    assert all("x=" not in i for i in response2.code_variants), (
        "The code variants should not contain the assignment of x, as it was already assigned in the first request."
    )

    # Now we can edit the second request to the same input, but in a different thread, to test whether the value of x is still stored.
    response3 = generate_full_response(
        "Now print the value of x without assigning it again.",
        chatbot=selected_chatbot,
        thread_id=response1.thread_id,
        fork_from_index=4,
    )

    # The thread_ids should differ
    assert response1.thread_id != response3.thread_id, (
        "The thread_id of the edited response should be different from the original response, but it is not. This means that the edit request was not handled correctly. Instead, both thread_ids are: "
        + response1.thread_id
    )
    delete_thread(response1.thread_id)
    delete_thread(response3.thread_id)

    # The code output should now contain 42 again, as the value of x should still be stored.
    assert any("42" in i for i in response3.codeoutput_variants), (
        "The code output should contain 42, as the value of x should still be stored, but it does not. Instead, it returned: "
        + ", ".join(response3.codeoutput_variants)
    )
    assert all("x=" not in i for i in response3.code_variants), (
        "The code variants should not contain the assignment of x, as it was already assigned in the first request and should still be stored. Instead, it returned: "
        + ", ".join(response3.code_variants)
    )


def test_get_user_threads_with_n():
    """Can the Frontend request a specific number of threads of a user?"""  # Since Version TODO
    # This test is again dependant on MongoDB being turned on.
    if should_test_mongo:
        # We'll ask for 12 threads and assume their format is correct as the other test already tested that.
        response = get_user_threads(num_threads=12)
        threads, num_threads = response
        assert num_threads >= 12, "User does not have 12 threads yet, cannot test."
        assert len(threads) == 12, "Expected 12 threads, but got: " + str(len(threads))


# Because this test suite was copied from the one written for the rust backend, it used to support that.
# There used to be a mock authentication here, but I've removed it because this test suite is now e2e only for the python backend
# and to avoid confusion.
