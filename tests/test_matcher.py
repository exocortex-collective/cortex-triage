"""Tests for match condition evaluation."""

import pytest

from triage.engine.matcher import (
    evaluate,
    matches_condition,
)
from triage.models.rules import (
    Action,
    Email,
    IntentConfig,
    LLMConfig,
    MatchCondition,
    Rule,
    RulesConfig,
)


@pytest.fixture
def sample_email() -> Email:
    """Sample email for testing."""
    return Email(
        id=1,
        gmail_id="abc123",
        from_addr="sender@example.com",
        to_addr="recipient@example.com",
        subject="Test Subject",
        body="This is the email body",
        has_attachment=False,
    )


class TestMatchesCondition:
    """Tests for matches_condition function."""

    def test_empty_condition_matches_all(self, sample_email: Email) -> None:
        """Empty condition should match any email."""
        condition = MatchCondition()
        assert matches_condition(condition, sample_email)

    def test_from_exact_match(self, sample_email: Email) -> None:
        """Test exact from address matching."""
        condition = MatchCondition(from_addr="sender@example.com")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(from_addr="other@example.com")
        assert not matches_condition(condition, sample_email)

    def test_from_case_insensitive(self, sample_email: Email) -> None:
        """Test from matching is case-insensitive."""
        condition = MatchCondition(from_addr="SENDER@EXAMPLE.COM")
        assert matches_condition(condition, sample_email)

    def test_to_exact_match(self, sample_email: Email) -> None:
        """Test exact to address matching."""
        condition = MatchCondition(to_addr="recipient@example.com")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(to_addr="other@example.com")
        assert not matches_condition(condition, sample_email)

    def test_from_glob(self, sample_email: Email) -> None:
        """Test glob pattern matching on from address."""
        condition = MatchCondition(from_glob="*@example.com")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(from_glob="*@other.com")
        assert not matches_condition(condition, sample_email)

        condition = MatchCondition(from_glob="sender@*")
        assert matches_condition(condition, sample_email)

    def test_to_glob(self, sample_email: Email) -> None:
        """Test glob pattern matching on to address."""
        condition = MatchCondition(to_glob="*@example.com")
        assert matches_condition(condition, sample_email)

    def test_from_contains(self, sample_email: Email) -> None:
        """Test substring matching on from address."""
        condition = MatchCondition(from_contains="sender")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(from_contains="example")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(from_contains="notfound")
        assert not matches_condition(condition, sample_email)

    def test_subject_contains_string(self, sample_email: Email) -> None:
        """Test subject contains with single string."""
        condition = MatchCondition(subject_contains="Test")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(subject_contains="notfound")
        assert not matches_condition(condition, sample_email)

    def test_subject_contains_list(self, sample_email: Email) -> None:
        """Test subject contains with list of strings."""
        condition = MatchCondition(subject_contains=["archive", "Test"])
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(subject_contains=["archive", "save"])
        assert not matches_condition(condition, sample_email)

    def test_subject_regex(self, sample_email: Email) -> None:
        """Test regex matching on subject."""
        condition = MatchCondition(subject_regex=r"Test\s+Subject")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(subject_regex=r"^\[.*\]")
        assert not matches_condition(condition, sample_email)

    def test_body_contains(self, sample_email: Email) -> None:
        """Test body contains matching."""
        condition = MatchCondition(body_contains="email body")
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(body_contains="notfound")
        assert not matches_condition(condition, sample_email)

    def test_body_contains_no_body(self) -> None:
        """Test body contains when email has no body."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addr="recipient@example.com",
            subject="Test",
            body=None,
        )
        condition = MatchCondition(body_contains="anything")
        assert not matches_condition(condition, email)

    def test_has_attachment(self, sample_email: Email) -> None:
        """Test attachment matching."""
        condition = MatchCondition(has_attachment=False)
        assert matches_condition(condition, sample_email)

        condition = MatchCondition(has_attachment=True)
        assert not matches_condition(condition, sample_email)

    def test_multiple_conditions_all_must_match(self, sample_email: Email) -> None:
        """Test that all specified conditions must match."""
        # Both match
        condition = MatchCondition(
            from_addr="sender@example.com",
            subject_contains="Test",
        )
        assert matches_condition(condition, sample_email)

        # One matches, one doesn't
        condition = MatchCondition(
            from_addr="sender@example.com",
            subject_contains="notfound",
        )
        assert not matches_condition(condition, sample_email)


class TestEvaluateChain:
    """Tests for chain evaluation."""

    def test_simple_action(self, sample_email: Email) -> None:
        """Test simple rule with action."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(from_glob="*@example.com"),
                        action=Action(label="Test"),
                    )
                ]
            }
        )
        result = evaluate(config, sample_email)
        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/Test"

    def test_no_match(self, sample_email: Email) -> None:
        """Test when no rules match."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(from_glob="*@other.com"),
                        action=Action(label="Test"),
                    )
                ]
            }
        )
        result = evaluate(config, sample_email)
        assert not result.matched
        assert result.action is None

    def test_first_match_wins(self, sample_email: Email) -> None:
        """Test that first matching rule wins."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(from_glob="*@example.com"),
                        action=Action(label="First"),
                    ),
                    Rule(
                        match=MatchCondition(),  # Matches all
                        action=Action(label="Second"),
                    ),
                ]
            }
        )
        result = evaluate(config, sample_email)
        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/First"

    def test_jump_to_chain(self, sample_email: Email) -> None:
        """Test jumping to another chain."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(from_glob="*@example.com"),
                        jump="other",
                    )
                ],
                "other": [
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="FromOther"),
                    )
                ],
            }
        )
        result = evaluate(config, sample_email)
        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/FromOther"
        assert result.chain == "other"

    def test_jump_chain_no_match_continues(self, sample_email: Email) -> None:
        """Test that if jump chain doesn't match, evaluation continues."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(),
                        jump="other",
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Fallback"),
                    ),
                ],
                "other": [
                    Rule(
                        match=MatchCondition(from_glob="*@nomatch.com"),
                        action=Action(label="Other"),
                    )
                ],
            }
        )
        result = evaluate(config, sample_email)
        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/Fallback"

    def test_cycle_detection(self, sample_email: Email) -> None:
        """Test that chain cycles are detected and don't infinite loop."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="a"),
                ],
                "a": [
                    Rule(match=MatchCondition(), jump="b"),
                ],
                "b": [
                    Rule(match=MatchCondition(), jump="a"),  # Cycle!
                ],
            }
        )
        # Should not hang, should return no match
        result = evaluate(config, sample_email)
        assert not result.matched

    def test_label_prefix_applied(self, sample_email: Email) -> None:
        """Test that label prefix is applied."""
        config = RulesConfig(
            label_prefix="MyPrefix",
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Category/Subcategory"),
                    )
                ]
            },
        )
        result = evaluate(config, sample_email)
        assert result.action is not None
        assert result.action.label == "MyPrefix/Category/Subcategory"

    def test_action_without_label(self, sample_email: Email) -> None:
        """Test action without label doesn't break."""
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(),
                        action=Action(archive=True, mark_read=True),
                    )
                ]
            }
        )
        result = evaluate(config, sample_email)
        assert result.matched
        assert result.action is not None
        assert result.action.archive is True
        assert result.action.mark_read is True
        assert result.action.label is None


class TestSelfNoteChain:
    """Tests simulating the self-note chain from spec."""

    def test_self_email_jumps_to_self_note(self) -> None:
        """Test self-to-self email jumps to self_note chain."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="Some note to myself",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(
                            from_addr="me@example.com",
                            to_addr="me@example.com",
                        ),
                        jump="self_note",
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Other"),
                    ),
                ],
                "self_note": [
                    Rule(
                        match=MatchCondition(subject_contains=["archive", "save"]),
                        action=Action(label="Archiver"),
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Self-Note"),
                    ),
                ],
            }
        )

        result = evaluate(config, email)
        assert result.matched
        assert result.chain == "self_note"
        assert result.action is not None
        assert result.action.label == "Cortex/Self-Note"

    def test_self_email_with_archive_subject(self) -> None:
        """Test self email with archive keyword gets Archiver label."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="archive this article",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(
                            from_addr="me@example.com",
                            to_addr="me@example.com",
                        ),
                        jump="self_note",
                    ),
                ],
                "self_note": [
                    Rule(
                        match=MatchCondition(subject_contains=["archive", "save"]),
                        action=Action(label="Archiver"),
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Self-Note"),
                    ),
                ],
            }
        )

        result = evaluate(config, email)
        assert result.matched
        assert result.action is not None
        # Label triggers actions service which dispatches to archiver workflow
        assert result.action.label == "Cortex/Archiver"


class MockLLMClient:
    """Mock LLM client for testing."""

    def __init__(
        self,
        intent_responses: dict[str, bool] | None = None,
        classify_response: tuple[str, float, str] | None = None,
    ):
        self.intent_responses = intent_responses or {}
        self.classify_response = classify_response or ("unknown", 0.5, "default")
        self.intent_calls: list[tuple[str, str, str]] = []
        self.classify_calls: list[tuple[str, str, str, str, str]] = []

    def check_intent(self, subject: str, prompt: str, model: str) -> bool:
        self.intent_calls.append((subject, prompt, model))
        # Return based on subject content for testing
        for key, value in self.intent_responses.items():
            if key.lower() in subject.lower():
                return value
        return False

    def classify(
        self, from_addr: str, subject: str, body: str, model: str, prompt_version: str
    ) -> tuple[str, float, str]:
        self.classify_calls.append((from_addr, subject, body, model, prompt_version))
        return self.classify_response


class TestLLMMatching:
    """Tests for LLM-based matching features."""

    def test_subject_intent_with_named_intent(self) -> None:
        """Test subject_intent matching with a named intent reference."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="archive this article",
        )
        condition = MatchCondition(subject_intent="archive_request")
        intents = {
            "archive_request": {
                "prompt": "Is this an archive request? Subject: {subject}",
                "model": "test-model",
            }
        }
        llm_client = MockLLMClient(intent_responses={"archive": True})

        result = matches_condition(condition, email, llm_client, intents)

        assert result is True
        assert len(llm_client.intent_calls) == 1
        assert "archive this article" in llm_client.intent_calls[0][0]

    def test_subject_intent_no_match(self) -> None:
        """Test subject_intent when intent doesn't match."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="random email subject",
        )
        condition = MatchCondition(subject_intent="archive_request")
        intents = {
            "archive_request": {
                "prompt": "Is this an archive request?",
                "model": "test-model",
            }
        }
        llm_client = MockLLMClient(intent_responses={"archive": True})

        result = matches_condition(condition, email, llm_client, intents)

        assert result is False

    def test_subject_intent_with_inline_config(self) -> None:
        """Test subject_intent with inline IntentConfig."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="save this for later",
        )
        condition = MatchCondition(
            subject_intent=IntentConfig(
                prompt="Is this a save request?",
                model="inline-model",
            )
        )
        llm_client = MockLLMClient(intent_responses={"save": True})

        result = matches_condition(condition, email, llm_client)

        assert result is True
        assert llm_client.intent_calls[0][2] == "inline-model"

    def test_subject_intent_without_llm_client(self) -> None:
        """Test subject_intent fails closed when no LLM client provided."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="archive this",
        )
        condition = MatchCondition(subject_intent="archive_request")
        intents = {"archive_request": {"prompt": "test", "model": "test"}}

        result = matches_condition(condition, email, llm_client=None, intents=intents)

        assert result is False

    def test_subject_intent_unknown_intent_reference(self) -> None:
        """Test subject_intent with unknown intent reference fails."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addr="me@example.com",
            subject="archive this",
        )
        condition = MatchCondition(subject_intent="nonexistent_intent")
        llm_client = MockLLMClient()

        result = matches_condition(condition, email, llm_client, intents={})

        assert result is False


class TestLLMClassification:
    """Tests for LLM classification chain evaluation."""

    def test_llm_classification_matches_route(self) -> None:
        """Test LLM classification with matching route."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addr="recipient@example.com",
            subject="Please respond to this",
            body="Important message",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        llm=LLMConfig(model="test-model", prompt_version="v1"),
                        routes={
                            "human_request": Action(label="NeedsResponse", star=True),
                            "automated": Action(label="Automated", archive=True),
                        },
                    )
                ]
            }
        )
        llm_client = MockLLMClient(
            classify_response=("human_request", 0.9, "This is a human request")
        )

        result = evaluate(config, email, llm_client)

        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/NeedsResponse"
        assert result.action.star is True
        assert result.llm_category == "human_request"
        assert result.llm_confidence == 0.9
        assert result.llm_reasoning == "This is a human request"

    def test_llm_classification_no_matching_route(self) -> None:
        """Test LLM classification when category not in routes."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addr="recipient@example.com",
            subject="Test",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        llm=LLMConfig(model="test-model", prompt_version="v1"),
                        routes={
                            "human_request": Action(label="NeedsResponse"),
                        },
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Fallback"),
                    ),
                ]
            }
        )
        llm_client = MockLLMClient(
            classify_response=("unknown_category", 0.5, "Unknown")
        )

        result = evaluate(config, email, llm_client)

        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/Fallback"

    def test_llm_classification_without_client(self) -> None:
        """Test LLM rule is skipped when no client provided."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addr="recipient@example.com",
            subject="Test",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        llm=LLMConfig(model="test-model", prompt_version="v1"),
                        routes={
                            "human_request": Action(label="NeedsResponse"),
                        },
                    ),
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Fallback"),
                    ),
                ]
            }
        )

        result = evaluate(config, email, llm_client=None)

        assert result.matched
        assert result.action is not None
        assert result.action.label == "Cortex/Fallback"
