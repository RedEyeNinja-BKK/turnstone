"""Offline pins of the SDK boundary behaviors the #937 retry design rests on.

Five facts, each probed against the REAL SDKs over mock/loopback
transports (no network, no live backend):

1. OpenAI v3's ``max_retries`` covers request time only — a mid-BODY death
   produces no re-request.  Since v3.14 the SDK wraps that death in its
   own ``APIConnectionError`` (the raw transport error survives as the
   wrapper's ``__cause__``); before v3.14 the raw ``httpx2.ReadError``
   escaped unwrapped.  Both shapes reach the production
   ``transport_guarded`` seam, which must normalize either one.
2. OpenAI v3's runtime-only legacy-client path preserves the old ``httpx``
   exception family when an application explicitly injects that client.
3. The Anthropic ``messages.stream()`` helper propagates the transport
   error RAW — Anthropic never wraps a body-read death in an SDK error
   class, so the Anthropic lane needs no unwrapping.
4. Closing an OpenAI v3 default client from another thread while a read is
   blocked (the ``ModelRegistry.reload()`` shape) completes safely; a later
   wire release surfaces as a transport error on the blocked ``next()``.
   The production ``transport_guarded`` seam must normalize it before the
   retry gate.
5. A request on an ALREADY-CLOSED client is a creation failure, and the
   shape it takes changed with the SDK: v3.0-v3.14 raise the SDK's
   retryable ``APIConnectionError`` (cause: ``RuntimeError``), while
   v3.15+ let the transport's own ``RuntimeError`` escape unwrapped.
   Either way it must stay distinguishable from a mid-stream death, and
   the ``transport_guarded`` seam must not mistake either for a wire blip.

If an SDK/httpx upgrade changes any of these, the ``transport_guarded``
conversion (and the retry gate consuming it) must be re-verified — these
tests are the tripwire.  Wire framing bytes (CRLF, the SSE double-LF) are
built via ``chr`` rather than escape literals so the payloads are
byte-exact regardless of source-encoding handling.

The Anthropic client family is chosen at import time from the INSTALLED
SDK: v1 runs on HTTPX2 and REJECTS an injected ``httpx.Client``, while
v0 runs on HTTPX and rejects ``httpx2.Client``.  Each family is probed
with the one it accepts, so this file measures the contract of whatever
SDK is installed rather than hard-coding one major.
"""

import contextlib
import socket
import threading
import time

import anthropic
import httpx
import httpx2
import openai
import pytest

LF = chr(10)
CRLF = chr(13) + chr(10)

CHAT_CHUNK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":0,'
    '"model":"m","choices":[{"index":0,"delta":{"content":"hello"},'
    '"finish_reason":null}]}' + LF + LF
)

RESPONSES_EVENT = (
    'data: {"type":"response.output_text.delta","sequence_number":0,'
    '"item_id":"item_1","output_index":0,"content_index":0,'
    '"delta":"hello","logprobs":[]}' + LF + LF
)

ANTHROPIC_EVENTS = (
    "event: message_start"
    + LF
    + 'data: {"type":"message_start","message":{"id":"msg_01X","type":"message",'
    '"role":"assistant","model":"m","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":1}}}'
    + LF
    + LF
    + "event: content_block_start"
    + LF
    + 'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}'
    + LF
    + LF
    + "event: content_block_delta"
    + LF
    + 'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}' + LF + LF
)


class _DyingStream(httpx.SyncByteStream):
    """Response body: one valid SSE payload, then a mid-read wire death."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __iter__(self):
        yield self._payload
        raise httpx.ReadError("[SSL] record layer failure (_ssl.c:2590)")


class _Httpx2DyingStream(httpx2.SyncByteStream):
    """HTTPX2 response body: one SSE payload, then a wire death."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __iter__(self):
        yield self._payload
        raise httpx2.ReadError("[SSL] record layer failure (_ssl.c:2590)")


def _dying_transport(payload: str, requests: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_DyingStream(payload.encode()),
            request=request,
        )

    return httpx.MockTransport(handler)


def _httpx2_dying_transport(payload: str, requests: list) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Httpx2DyingStream(payload.encode()),
            request=request,
        )

    return httpx2.MockTransport(handler)


# The Anthropic SDK's HTTP family is a function of its MAJOR: v1 runs on
# HTTPX2 and REJECTS an injected ``httpx.Client`` at construction, while
# v0 runs on HTTPX and rejects ``httpx2.Client``.  Each family is probed
# with the one it accepts, so this module measures the contract of the
# INSTALLED SDK instead of hard-coding one major.
_ANTHROPIC_V1 = int(anthropic.__version__.split(".")[0]) >= 1
_ANTHROPIC_TRANSPORT_ERROR: type[BaseException] = (
    httpx2.TransportError if _ANTHROPIC_V1 else httpx.TransportError
)


def _anthropic_client(requests: list) -> anthropic.Anthropic:
    """An Anthropic client over the transport family the installed SDK accepts."""
    if _ANTHROPIC_V1:
        transport = _httpx2_dying_transport(ANTHROPIC_EVENTS, requests)
        return anthropic.Anthropic(
            api_key="probe",
            base_url="http://probe.invalid",
            http_client=httpx2.Client(transport=transport),
            max_retries=2,
        )
    transport = _dying_transport(ANTHROPIC_EVENTS, requests)
    return anthropic.Anthropic(
        api_key="probe",
        base_url="http://probe.invalid",
        http_client=httpx.Client(transport=transport),
        max_retries=2,
    )


def _openai_transport_error(exc: BaseException) -> BaseException:
    """The transport error an OpenAI SDK stream death carries.

    OpenAI v3.14+ wraps a body-read death in the SDK's ``APIConnectionError``
    and keeps the transport error as its ``__cause__``; before v3.14 the
    transport error escaped raw.  Both shapes reach the production seam, so
    the assertion walks the cause chain to the transport error itself.
    """
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, (httpx.TransportError, httpx2.TransportError)):
            return current
        current = current.__cause__
    raise AssertionError(f"no transport error in the chain of {exc!r}")


def test_openai_v3_chat_midbody_death_carries_httpx2_error_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(CHAT_CHUNK, requests)),
        max_retries=2,
    )
    stream = client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    texts = []
    with pytest.raises(BaseException) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    # The retry gate matches on the class NAME; pin the transport error the
    # SDK lets escape, and that it is the HTTPX2 transport family.
    transport_exc = _openai_transport_error(excinfo.value)
    assert type(transport_exc).__name__ == "ReadError"
    assert isinstance(transport_exc, httpx2.TransportError)
    assert texts == ["hello"]  # the request succeeded; the BODY died
    assert len(requests) == 1  # max_retries never re-requested mid-body


def test_openai_v3_responses_midbody_death_carries_httpx2_error_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(RESPONSES_EVENT, requests)),
        max_retries=2,
    )
    stream = client.responses.create(model="m", input="hi", stream=True)
    texts = []
    with pytest.raises(BaseException) as excinfo:
        for event in stream:
            if event.type == "response.output_text.delta":
                texts.append(event.delta)
    transport_exc = _openai_transport_error(excinfo.value)
    assert type(transport_exc).__name__ == "ReadError"
    assert isinstance(transport_exc, httpx2.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


def test_openai_v3_legacy_httpx_midbody_death_keeps_legacy_error_family():
    requests: list = []
    legacy_http_client = httpx.Client(transport=_dying_transport(CHAT_CHUNK, requests))
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=legacy_http_client,  # type: ignore[arg-type]
        max_retries=2,
    )
    stream = client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    texts = []
    with pytest.raises(BaseException) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    transport_exc = _openai_transport_error(excinfo.value)
    assert type(transport_exc).__name__ == "ReadError"
    assert isinstance(transport_exc, httpx.TransportError)
    assert not isinstance(transport_exc, httpx2.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


def test_anthropic_midbody_death_is_unwrapped_readerror_and_no_rerequest():
    requests: list = []
    client = _anthropic_client(requests)
    texts = []
    # The provider adapter consumes the stream() helper's iterator — same
    # shape as _anthropic.py's create_streaming.
    with (
        client.messages.stream(
            model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
        ) as stream,
        pytest.raises(BaseException) as excinfo,
    ):
        for event in stream:
            if getattr(event, "type", "") == "content_block_delta":
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", "") == "text_delta":
                    texts.append(delta.text)
    # Anthropic does NOT wrap a body-read death: the transport error
    # escapes raw, so the production seam sees it directly.
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, _ANTHROPIC_TRANSPORT_ERROR)
    assert texts == ["hello"]
    assert len(requests) == 1


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_closed_v3_default_client_creation_stays_unarmed(surface: str):
    """A re-create on the client closed by reload is still a creation error.

    The request never reaches the wire, so neither adapter can arm the
    stream: the creation-vs-mid-stream classifier stays intact.  The SDK's
    shape for that failure changed with the SDK (v3.0-v3.14 raise the
    retryable ``APIConnectionError`` with a ``RuntimeError`` cause; v3.15+
    let the transport's ``RuntimeError`` escape unwrapped), so the pin is
    on the ARMING and on the class, not on one SDK's exact wrapper.
    """
    from turnstone.core.providers import create_provider

    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        max_retries=0,
    )
    client.close()
    cancel_ref: list = []
    provider = create_provider("openai-compatible", api_surface=surface)

    with pytest.raises(BaseException) as excinfo:
        provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=cancel_ref,
        )

    assert cancel_ref == []
    # A creation failure on a closed client is NOT a mid-stream death: it
    # must not be normalized into a transport error, and the transport
    # guard must not absorb it as a post-finish blip.
    assert not isinstance(excinfo.value, httpx.TransportError)
    assert not isinstance(excinfo.value, httpx2.TransportError)
    assert type(excinfo.value).__name__ in {
        "APIConnectionError",
        "RuntimeError",
    }


@pytest.mark.parametrize(
    ("surface", "payload"),
    [("chat", CHAT_CHUNK), ("responses", RESPONSES_EVENT)],
)
def test_cross_thread_v3_default_client_close_then_wire_release_is_normalized(
    surface: str, payload: str
):
    """The ``ModelRegistry.reload()`` shape stays safe at the retry seam.

    OpenAI v3's synchronous HTTPX2 client does not promise that cross-thread
    ``close()`` itself interrupts a blocked body read. Pin the behavior
    Turnstone needs instead: closing from the admin thread completes safely
    while a worker is in ``next()``, and the subsequent wire release reaches
    ``transport_guarded`` as a provider-retryable ``IncompleteStreamError``
    for both OpenAI streaming adapters.
    """
    from turnstone.core.providers import create_provider, transport_guarded
    from turnstone.core.providers._protocol import IncompleteStreamError

    body = f"{len(payload):x}" + CRLF + payload + CRLF
    response_head = (
        "HTTP/1.1 200 OK"
        + CRLF
        + "Content-Type: text/event-stream"
        + CRLF
        + "Transfer-Encoding: chunked"
        + CRLF
        + CRLF
    )
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(10.0)
    port = listener.getsockname()[1]
    release_peer = threading.Event()
    reader_blocked = threading.Event()
    close_done = threading.Event()
    first_content: list[str] = []
    reader_errors: list[BaseException] = []
    closer_errors: list[BaseException] = []
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(10.0)
                conn.recv(65536)
                conn.sendall((response_head + body).encode())
                # Keep the peer open through close_done: any reader error
                # before release_peer is therefore caused by close(), not EOF.
                if not release_peer.wait(timeout=15.0):
                    raise AssertionError("peer release was never signalled")
        except BaseException as exc:
            server_errors.append(exc)

    client = openai.OpenAI(
        api_key="probe",
        base_url=f"http://127.0.0.1:{port}/v1",
        max_retries=0,
        timeout=5.0,
    )

    def read_stream() -> None:
        try:
            provider = create_provider("openai-compatible", api_surface=surface)
            chunks = provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
            it = transport_guarded(chunks)
            first_content.append(next(it).content_delta)
            reader_blocked.set()
            next(it)
        except BaseException as exc:
            reader_errors.append(exc)

    def closer() -> None:
        try:
            if not reader_blocked.wait(timeout=10.0):
                raise AssertionError("reader never reached the blocked body read")
            time.sleep(0.5)  # let the reader enter the blocking socket read
            client.close()
        except BaseException as exc:
            closer_errors.append(exc)
        finally:
            close_done.set()

    server_thread = threading.Thread(target=serve)
    reader_thread = threading.Thread(target=read_stream)
    closer_thread = threading.Thread(target=closer)
    server_thread.start()
    reader_thread.start()
    closer_thread.start()
    try:
        assert reader_blocked.wait(timeout=10.0)
        assert close_done.wait(timeout=10.0)
        assert closer_errors == []
        # The server has not closed its peer yet, proving close() completed
        # safely rather than merely returning after an EOF unblocked it.
        assert server_errors == []
        release_peer.set()
        reader_thread.join(timeout=10.0)
    finally:
        # Unblock and join every thread on every exit path so nothing
        # outlives the test (leaked-thread guard).
        reader_blocked.set()
        release_peer.set()
        closer_thread.join(timeout=10.0)
        reader_thread.join(timeout=10.0)
        server_thread.join(timeout=10.0)
        listener.close()
        with contextlib.suppress(Exception):
            client.close()
    assert first_content == ["hello"]
    assert len(reader_errors) == 1
    exc = reader_errors[0]
    assert isinstance(exc, IncompleteStreamError)
    assert any(name in str(exc) for name in ("ReadError", "RemoteProtocolError"))
    # The cause is the transport error, possibly wrapped by the SDK (v3.14+
    # wraps a body-read death in its own APIConnectionError) — the
    # production seam unwraps it, so the normalized cause is the transport
    # error itself.
    cause = exc.__cause__
    assert isinstance(cause, httpx2.TransportError)
    assert type(cause).__name__ in {"ReadError", "RemoteProtocolError"}
    assert server_errors == []
    assert not closer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert not server_thread.is_alive()


class TestAnthropicRequestShape:
    """The Anthropic request shape must survive the SDK's typing change.

    Anthropic SDK v1 REMOVED ``temperature``/``top_p``/``top_k`` from the
    Messages signatures, so a typed ``temperature=`` is a TypeError before
    any request is issued.  The provider carries the value in
    ``extra_body``, which puts the SAME key on the wire — measured
    byte-identical request bodies on 0.121.0 (the locked floor) and
    1.15.0 (the float CI installs).

    These tests drive the REAL SDK over a mock transport and inspect the
    REQUEST BODY, so they pin the wire shape rather than the kwargs the
    provider happened to build.
    """

    def _capture(self, model: str, temperature: float | None) -> dict:
        import json

        from turnstone.core.providers._anthropic import AnthropicProvider

        captured: dict = {}
        # A minimal but VALID message_start event: the SDK parses the
        # response body, so the stub must be a real SSE frame.
        frame = (
            "event: message_start"
            + LF
            + 'data: {"type":"message_start","message":{"id":"m","type":"message",'
            '"role":"assistant","model":"m","content":[],"stop_reason":null,'
            '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":1}}}' + LF + LF
        ).encode()

        if _ANTHROPIC_V1:

            class _S(httpx2.SyncByteStream):
                def __iter__(self):
                    yield frame

            def handler(request: httpx2.Request) -> httpx2.Response:
                captured["body"] = request.content.decode()
                return httpx2.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_S(),
                    request=request,
                )

            client = anthropic.Anthropic(
                api_key="probe",
                base_url="http://probe.invalid",
                http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            )
        else:

            class _S(httpx.SyncByteStream):
                def __iter__(self):
                    yield frame

            def handler(request: httpx.Request) -> httpx.Response:
                captured["body"] = request.content.decode()
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_S(),
                    request=request,
                )

            client = anthropic.Anthropic(
                api_key="probe",
                base_url="http://probe.invalid",
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )

        provider = AnthropicProvider()
        caps = provider.get_capabilities(model)
        kwargs = provider._build_thinking_and_kwargs(
            caps,
            None,
            {},
            4096,
            temperature,
            [{"role": "user", "content": "hi"}],
            "",
            model,
            None,
        )
        with client.messages.stream(**kwargs) as stream:
            for _ in stream:
                pass
        return json.loads(captured["body"])

    def test_temperature_reaches_the_wire(self):
        """A model whose capabilities allow temperature carries it to the wire."""
        body = self._capture("claude-sonnet-4-6", 0.5)
        assert body["temperature"] == 1.0  # the thinking branch forces 1.0

    def test_a_model_without_temperature_support_omits_it(self):
        body = self._capture("claude-opus-5", 0.5)
        assert "temperature" not in body


class TestWrappedTransportDeath:
    """The SDK's WRAPPED transport death must normalize like a raw one.

    OpenAI v3.14+ wraps a body-read failure in its own ``APIConnectionError``
    (the transport error survives as the wrapper's ``__cause__``).  The
    production seam's one conversion rule has to see through that wrapper:
    without the unwrap, a post-finish blip would ABORT an already-completed
    generation instead of forfeiting only its trailing metadata, and a
    mid-body death would escape unnormalized.

    These are the regression pins for that unwrap, driven through the REAL
    SDK (the wrapper is the SDK's, not a hand-built stand-in).
    """

    def _dying_chat_client(self, requests: list) -> openai.OpenAI:
        return openai.OpenAI(
            api_key="probe",
            base_url="http://probe.invalid/v1",
            http_client=httpx2.Client(transport=_httpx2_dying_transport(CHAT_CHUNK, requests)),
            max_retries=0,
        )

    def test_mid_body_death_normalizes_to_incomplete_stream(self):
        from turnstone.core.providers import create_provider, transport_guarded
        from turnstone.core.providers._protocol import IncompleteStreamError

        requests: list = []
        client = self._dying_chat_client(requests)
        provider = create_provider("openai-compatible", api_surface="chat")
        chunks = provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
        )
        it = transport_guarded(chunks)
        with pytest.raises(IncompleteStreamError) as excinfo:
            for _ in it:
                pass
        # The normalized cause is the TRANSPORT error, not the SDK wrapper.
        cause = excinfo.value.__cause__
        assert isinstance(cause, httpx2.TransportError)
        assert type(cause).__name__ == "ReadError"
        assert len(requests) == 1

    def test_post_finish_blip_ends_the_stream_cleanly(self, caplog):
        """A completed generation must survive a trailing wire death.

        The SDK wraps the death, so the guard has to unwrap it to recognise
        the post-finish tolerance.  Without the unwrap the wrapper escapes
        the guard and the completed generation is lost.
        """
        import logging

        from turnstone.core.providers import transport_guarded
        from turnstone.core.providers._protocol import StreamChunk

        # A finish, then a wrapped transport death: the shape the SDK
        # produces after the generation completed (the SDK raises the
        # wrapper FROM the transport error).
        finished = StreamChunk(content_delta="done")
        finished.finish_reason = "stop"

        def chunks():
            yield finished
            raise openai.APIConnectionError(
                request=httpx2.Request("GET", "http://probe.invalid/v1")
            ) from httpx2.ReadError("[SSL] record layer failure (_ssl.c:2590)")

        with caplog.at_level(logging.WARNING, logger="turnstone.core.providers._protocol"):
            out = list(transport_guarded(chunks()))
        assert [c.content_delta for c in out] == ["done"]
        assert out[-1].finish_reason == "stop"
        blips = [r.message for r in caplog.records if "stream.post_finish_blip" in r.message]
        assert blips

    def test_wrapped_death_without_a_transport_cause_is_not_absorbed(self):
        """A wrapper with NO transport cause is not a wire blip.

        Only a real transport error (or one a wrapper carries) may be
        absorbed as a post-finish blip.  An unrelated failure that merely
        LOOKS like the SDK's wrapper must still propagate.
        """
        from turnstone.core.providers import transport_guarded
        from turnstone.core.providers._protocol import StreamChunk

        finished = StreamChunk(content_delta="done")
        finished.finish_reason = "stop"

        def chunks():
            yield finished
            raise openai.APIConnectionError(
                request=httpx2.Request("GET", "http://probe.invalid/v1")
            ) from ValueError("not a transport failure")

        with pytest.raises(openai.APIConnectionError):
            list(transport_guarded(chunks()))


class TestEagerAppendContract:
    """Every adapter arms ``cancel_ref`` INSIDE ``create_streaming``'s body —
    at HTTP-response time, before the iterator is returned (the Protocol
    contract, strengthened on #832): the interactive wrapper's
    creation-vs-midstream classifier and its health recording key on that
    instant, and a lazily-issued generator adapter would silently move the
    arming to first ``next()``, misclassifying every pre-first-chunk death
    as a creation failure.  Real SDK clients over mock transports; the
    assertion deliberately runs BEFORE any iteration.
    """

    def _armed_at_return(self, provider, client, **extra):
        ref: list = []
        stream = provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=ref,
            **extra,
        )
        assert len(ref) == 1, "cancel_ref not armed before create_streaming returned"
        assert hasattr(ref[0], "close")
        with contextlib.suppress(Exception):
            stream.close()

    def test_openai_chat_arms_eagerly(self):
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        requests: list = []
        client = openai.OpenAI(
            api_key="probe",
            http_client=httpx2.Client(transport=_httpx2_dying_transport(CHAT_CHUNK, requests)),
        )
        self._armed_at_return(OpenAIChatCompletionsProvider(), client)
        assert len(requests) == 1  # the HTTP call happened inside create

    def test_openai_responses_arms_eagerly(self):
        from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

        requests: list = []
        client = openai.OpenAI(
            api_key="probe",
            http_client=httpx2.Client(transport=_httpx2_dying_transport(RESPONSES_EVENT, requests)),
        )
        self._armed_at_return(OpenAIResponsesProvider(), client)
        assert len(requests) == 1

    def test_anthropic_arms_eagerly(self):
        from turnstone.core.providers._anthropic import AnthropicProvider

        requests: list = []
        client = _anthropic_client(requests)
        self._armed_at_return(AnthropicProvider(), client)
        assert len(requests) == 1
