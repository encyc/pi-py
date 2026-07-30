from pi_ai import AssistantMessage, StreamOptions


def test_assistant_message_pending_and_raw_stop_reason_round_trip():
    message = AssistantMessage(stopReason="pending", rawStopReason="end_turn")

    assert message.stop_reason == "pending"
    assert message.raw_stop_reason == "end_turn"
    dumped = message.model_dump(by_alias=True)
    assert dumped["stopReason"] == "pending"
    assert dumped["rawStopReason"] == "end_turn"


def test_assistant_message_defaults_to_pending():
    assert AssistantMessage().stop_reason == "pending"


def test_stream_options_accepts_external_http_client_without_serializing_it():
    sentinel = object()
    options = StreamOptions(httpClient=sentinel)

    assert options.http_client is sentinel
    assert "httpClient" not in options.model_dump(by_alias=True)
