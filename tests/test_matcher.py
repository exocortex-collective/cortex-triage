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
        to_addrs=["recipient@example.com"],
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
            to_addrs=["recipient@example.com"],
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
            to_addrs=["me@example.com"],
            subject="Some note to myself",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(
                            from_addr="me@example.com",
                            to_addrs=["me@example.com"],
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
            to_addrs=["me@example.com"],
            subject="archive this article",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(
                        match=MatchCondition(
                            from_addr="me@example.com",
                            to_addrs=["me@example.com"],
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
        email_intent_responses: dict[str, bool] | None = None,
        categorize_response: str | None = None,
    ):
        self.intent_responses = intent_responses or {}
        self.classify_response = classify_response or ("unknown", 0.5, "default")
        self.email_intent_responses = email_intent_responses or {}
        self.categorize_response = categorize_response
        self.intent_calls: list[tuple[str, str, str]] = []
        self.classify_calls: list[tuple[str, str]] = []
        self.email_intent_calls: list[tuple[str, str, str | None, str, str]] = []
        self.categorize_calls: list[
            tuple[str, str, str | None, str, str, list[str]]
        ] = []

    def check_intent(self, subject: str, prompt: str, model: str) -> bool:
        self.intent_calls.append((subject, prompt, model))
        # Return based on subject content for testing
        for key, value in self.intent_responses.items():
            if key.lower() in subject.lower():
                return value
        return False

    def check_email_intent(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> bool:
        self.email_intent_calls.append((from_addr, subject, body, prompt, model))
        # Return based on email content for testing
        content = f"{from_addr} {subject} {body or ''}"
        for key, value in self.email_intent_responses.items():
            if key.lower() in content.lower():
                return value
        return False

    def categorize_email(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
        categories: list[str],
    ) -> str | None:
        self.categorize_calls.append(
            (from_addr, subject, body, prompt, model, categories)
        )
        return self.categorize_response

    def classify(self, prompt: str, model: str) -> tuple[str, float, str]:
        self.classify_calls.append((prompt, model))
        return self.classify_response


class TestLLMMatching:
    """Tests for LLM-based matching features."""

    def test_subject_intent_with_named_intent(self) -> None:
        """Test subject_intent matching with a named intent reference."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="me@example.com",
            to_addrs=["me@example.com"],
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
            to_addrs=["me@example.com"],
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
            to_addrs=["me@example.com"],
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
            to_addrs=["me@example.com"],
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
            to_addrs=["me@example.com"],
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
            to_addrs=["recipient@example.com"],
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
            to_addrs=["recipient@example.com"],
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
            to_addrs=["recipient@example.com"],
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


class TestListFields:
    """Tests for list-based field matching (OR within field)."""

    def test_from_addr_list_matches_any(self) -> None:
        """Test that from_addr list matches any address."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="newsletter@foo.com",
            to_addrs=["me@example.com"],
            subject="Weekly digest",
        )
        condition = MatchCondition(
            from_addr=["newsletter@foo.com", "deals@bar.com", "promo@baz.com"]
        )

        assert matches_condition(condition, email) is True

    def test_from_addr_list_no_match(self) -> None:
        """Test that from_addr list fails when no address matches."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="random@other.com",
            to_addrs=["me@example.com"],
            subject="Hello",
        )
        condition = MatchCondition(from_addr=["newsletter@foo.com", "deals@bar.com"])

        assert matches_condition(condition, email) is False

    def test_from_glob_list_matches_any(self) -> None:
        """Test that from_glob list matches any pattern."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="news@mailchimp.com",
            to_addrs=["me@example.com"],
            subject="Campaign update",
        )
        condition = MatchCondition(
            from_glob=["*@mailchimp.com", "*@sendgrid.net", "*@constantcontact.com"]
        )

        assert matches_condition(condition, email) is True

    def test_to_addr_list(self) -> None:
        """Test to_addr with list of addresses."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addrs=["work@mycompany.com"],
            subject="Meeting",
        )
        condition = MatchCondition(to_addr=["personal@gmail.com", "work@mycompany.com"])

        assert matches_condition(condition, email) is True

    def test_subject_regex_list_matches_any(self) -> None:
        """Test that subject_regex list matches any pattern."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Application crashes on startup",
        )
        condition = MatchCondition(
            subject_regex=[r"\[Bug\]", r"\[Critical\]", r"\[Urgent\]"]
        )

        assert matches_condition(condition, email) is True

    def test_list_with_other_conditions_and(self) -> None:
        """Test list field combined with other conditions (AND)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Crash in module X",
        )
        # Must be from github AND subject contains bug
        condition = MatchCondition(
            from_glob=["*@github.com", "*@gitlab.com"],
            subject_contains="bug",
        )

        assert matches_condition(condition, email) is True

        # Different domain - fails the from_glob
        email2 = Email(
            id=2,
            gmail_id="def456",
            from_addr="alerts@bitbucket.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Another issue",
        )
        assert matches_condition(condition, email2) is False


class TestCompoundConditions:
    """Tests for any_of and all_of compound conditions."""

    def test_any_of_matches_first(self) -> None:
        """Test any_of matches when first condition matches."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="news@mailchimp.com",
            to_addrs=["me@example.com"],
            subject="Newsletter",
        )
        condition = MatchCondition(
            any_of=[
                MatchCondition(from_glob="*@mailchimp.com"),
                MatchCondition(from_glob="*@sendgrid.net"),
                MatchCondition(from_addr="newsletter@example.com"),
            ]
        )

        assert matches_condition(condition, email) is True

    def test_any_of_matches_middle(self) -> None:
        """Test any_of matches when middle condition matches."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="campaign@sendgrid.net",
            to_addrs=["me@example.com"],
            subject="Promo",
        )
        condition = MatchCondition(
            any_of=[
                MatchCondition(from_glob="*@mailchimp.com"),
                MatchCondition(from_glob="*@sendgrid.net"),
                MatchCondition(from_addr="newsletter@example.com"),
            ]
        )

        assert matches_condition(condition, email) is True

    def test_any_of_no_match(self) -> None:
        """Test any_of fails when no condition matches."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="friend@personal.com",
            to_addrs=["me@example.com"],
            subject="Hello",
        )
        condition = MatchCondition(
            any_of=[
                MatchCondition(from_glob="*@mailchimp.com"),
                MatchCondition(from_glob="*@sendgrid.net"),
            ]
        )

        assert matches_condition(condition, email) is False

    def test_all_of_all_match(self) -> None:
        """Test all_of succeeds when all conditions match."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Critical issue in production",
        )
        condition = MatchCondition(
            all_of=[
                MatchCondition(from_glob="*@github.com"),
                MatchCondition(subject_contains="bug"),
                MatchCondition(subject_contains="critical"),
            ]
        )

        assert matches_condition(condition, email) is True

    def test_all_of_one_fails(self) -> None:
        """Test all_of fails when one condition doesn't match."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Minor issue",
        )
        condition = MatchCondition(
            all_of=[
                MatchCondition(from_glob="*@github.com"),
                MatchCondition(subject_contains="bug"),
                MatchCondition(subject_contains="critical"),  # Not in subject
            ]
        )

        assert matches_condition(condition, email) is False

    def test_any_of_with_other_conditions(self) -> None:
        """Test any_of combined with other field conditions (AND)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Issue in repo",
        )
        # Must be from github AND (subject contains bug OR issue)
        condition = MatchCondition(
            from_glob="*@github.com",
            any_of=[
                MatchCondition(subject_contains="bug"),
                MatchCondition(subject_contains="error"),
            ],
        )

        assert matches_condition(condition, email) is True

        # Wrong domain - fails the from_glob, even though any_of would match
        email2 = Email(
            id=2,
            gmail_id="def456",
            from_addr="alerts@gitlab.com",
            to_addrs=["me@example.com"],
            subject="[Bug] Issue",
        )
        assert matches_condition(condition, email2) is False

    def test_nested_compound_conditions(self) -> None:
        """Test nested any_of/all_of conditions."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="alerts@github.com",
            to_addrs=["dev@mycompany.com"],
            subject="[Bug] Critical failure",
        )
        # (from github OR gitlab) AND (subject has bug AND critical)
        condition = MatchCondition(
            any_of=[
                MatchCondition(from_glob="*@github.com"),
                MatchCondition(from_glob="*@gitlab.com"),
            ],
            all_of=[
                MatchCondition(subject_contains="bug"),
                MatchCondition(subject_contains="critical"),
            ],
        )

        assert matches_condition(condition, email) is True

        # From github but missing "critical"
        email2 = Email(
            id=2,
            gmail_id="def456",
            from_addr="alerts@github.com",
            to_addrs=["dev@mycompany.com"],
            subject="[Bug] Minor issue",
        )
        assert matches_condition(condition, email2) is False

    def test_empty_any_of_validation(self) -> None:
        """Test that empty any_of raises validation error."""
        import pytest

        with pytest.raises(ValueError, match="any_of must contain at least one"):
            MatchCondition(any_of=[])

    def test_empty_all_of_validation(self) -> None:
        """Test that empty all_of raises validation error."""
        import pytest

        with pytest.raises(ValueError, match="all_of must contain at least one"):
            MatchCondition(all_of=[])


class TestEmailIntent:
    """Tests for email_intent matching (full email context)."""

    def test_email_intent_with_named_intent(self) -> None:
        """Test email_intent matching with a named intent reference."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="noreply@amazon.com",
            to_addrs=["me@example.com"],
            subject="Your order has shipped",
            body="Track your package at...",
        )
        condition = MatchCondition(email_intent="transactional")
        intents = {
            "transactional": {
                "prompt": (
                    "Is this a transactional email? "
                    "From: {from_addr} Subject: {subject}"
                ),
                "model": "test-model",
            }
        }
        llm_client = MockLLMClient(email_intent_responses={"amazon": True})

        result = matches_condition(condition, email, llm_client, intents)

        assert result is True
        assert len(llm_client.email_intent_calls) == 1
        assert llm_client.email_intent_calls[0][0] == "noreply@amazon.com"

    def test_email_intent_no_match(self) -> None:
        """Test email_intent when intent doesn't match."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="friend@personal.com",
            to_addrs=["me@example.com"],
            subject="Hey, how are you?",
            body="Just checking in...",
        )
        condition = MatchCondition(email_intent="transactional")
        intents = {
            "transactional": {
                "prompt": "Is this transactional?",
                "model": "test-model",
            }
        }
        llm_client = MockLLMClient(email_intent_responses={"amazon": True})

        result = matches_condition(condition, email, llm_client, intents)

        assert result is False

    def test_email_intent_with_inline_config(self) -> None:
        """Test email_intent with inline IntentConfig."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="billing@stripe.com",
            to_addrs=["me@example.com"],
            subject="Invoice for December",
            body="Amount: $99.00",
        )
        condition = MatchCondition(
            email_intent=IntentConfig(
                prompt="Is this an invoice?",
                model="inline-model",
            )
        )
        llm_client = MockLLMClient(email_intent_responses={"stripe": True})

        result = matches_condition(condition, email, llm_client)

        assert result is True
        assert llm_client.email_intent_calls[0][4] == "inline-model"

    def test_email_intent_without_llm_client(self) -> None:
        """Test email_intent fails closed when no LLM client provided."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="noreply@company.com",
            to_addrs=["me@example.com"],
            subject="Receipt",
        )
        condition = MatchCondition(email_intent="transactional")
        intents = {"transactional": {"prompt": "test", "model": "test"}}

        result = matches_condition(condition, email, llm_client=None, intents=intents)

        assert result is False


class TestEmailCategory:
    """Tests for email_category matching (multi-class classifier)."""

    def test_email_category_matches(self) -> None:
        """Test email_category matching when category matches."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="billing@stripe.com",
            to_addrs=["me@example.com"],
            subject="Invoice #12345",
            body="Amount due: $99.00",
        )
        condition = MatchCondition(email_category="transactional/invoice")
        email_categories = {
            "transactional": {
                "prompt": "Categorize: {from_addr} {subject} Categories: {categories}",
                "model": "test-model",
                "categories": ["invoice", "receipt", "shipping", "other"],
            }
        }
        llm_client = MockLLMClient(categorize_response="invoice")

        result = matches_condition(
            condition, email, llm_client, intents={}, email_categories=email_categories
        )

        assert result is True
        assert len(llm_client.categorize_calls) == 1
        assert llm_client.categorize_calls[0][5] == [
            "invoice",
            "receipt",
            "shipping",
            "other",
        ]

    def test_email_category_no_match(self) -> None:
        """Test email_category when category doesn't match expected value."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="noreply@fedex.com",
            to_addrs=["me@example.com"],
            subject="Your package is on the way",
            body="Track your shipment...",
        )
        condition = MatchCondition(email_category="transactional/invoice")
        email_categories = {
            "transactional": {
                "prompt": "Categorize this email",
                "model": "test-model",
                "categories": ["invoice", "receipt", "shipping", "other"],
            }
        }
        llm_client = MockLLMClient(categorize_response="shipping")

        result = matches_condition(
            condition, email, llm_client, intents={}, email_categories=email_categories
        )

        assert result is False

    def test_email_category_invalid_format(self) -> None:
        """Test email_category with invalid format (missing /)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        condition = MatchCondition(email_category="invalid_no_slash")
        llm_client = MockLLMClient()

        result = matches_condition(
            condition, email, llm_client, intents={}, email_categories={}
        )

        assert result is False

    def test_email_category_unknown_category_name(self) -> None:
        """Test email_category with unknown category name."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        condition = MatchCondition(email_category="unknown_category/value")
        email_categories = {
            "transactional": {
                "prompt": "test",
                "model": "test",
                "categories": ["a", "b"],
            }
        }
        llm_client = MockLLMClient()

        result = matches_condition(
            condition, email, llm_client, intents={}, email_categories=email_categories
        )

        assert result is False

    def test_email_category_without_llm_client(self) -> None:
        """Test email_category fails closed when no LLM client provided."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        condition = MatchCondition(email_category="transactional/invoice")
        email_categories = {
            "transactional": {
                "prompt": "test",
                "model": "test",
                "categories": ["invoice"],
            }
        }

        result = matches_condition(
            condition,
            email,
            llm_client=None,
            intents={},
            email_categories=email_categories,
        )

        assert result is False

    def test_email_category_llm_returns_none(self) -> None:
        """Test email_category when LLM returns None (invalid category)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        condition = MatchCondition(email_category="transactional/invoice")
        email_categories = {
            "transactional": {
                "prompt": "test",
                "model": "test",
                "categories": ["invoice", "receipt"],
            }
        }
        llm_client = MockLLMClient(categorize_response=None)

        result = matches_condition(
            condition, email, llm_client, intents={}, email_categories=email_categories
        )

        assert result is False


class TestReturnToParent:
    """Tests for return_to_parent rule outcome."""

    def test_return_to_parent_basic(self) -> None:
        """Test basic return_to_parent exits chain and continues in parent."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="project@kickstarter.com",
            to_addrs=["me@example.com"],
            subject="Project Update: Cool Thing",
        )
        config = RulesConfig(
            chains={
                "main": [
                    # Jump to transactional for all emails
                    Rule(
                        match=MatchCondition(),
                        jump="transactional",
                    ),
                    # Fallback if transactional returns
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Other"),
                    ),
                ],
                "transactional": [
                    # Kickstarter project updates are not transactional - return
                    Rule(
                        match=MatchCondition(from_contains="kickstarter"),
                        return_to_parent=True,
                    ),
                    # Everything else in transactional chain
                    Rule(
                        match=MatchCondition(),
                        action=Action(label="Transactional"),
                    ),
                ],
            }
        )

        result = evaluate(config, email)

        assert result.matched
        assert result.action is not None
        # Should match the "Other" fallback in main chain after return
        assert result.action.label == "Cortex/Other"
        assert result.chain == "main"

    def test_return_to_parent_nested_chains(self) -> None:
        """Test return_to_parent from nested chains (A -> B -> return, A continues)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="special@example.com",
            to_addrs=["me@example.com"],
            subject="Special case",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="chain_a"),
                    Rule(match=MatchCondition(), action=Action(label="MainFallback")),
                ],
                "chain_a": [
                    Rule(match=MatchCondition(), jump="chain_b"),
                    Rule(match=MatchCondition(), action=Action(label="ChainAFallback")),
                ],
                "chain_b": [
                    # Return immediately
                    Rule(match=MatchCondition(), return_to_parent=True),
                ],
            }
        )

        result = evaluate(config, email)

        assert result.matched
        assert result.action is not None
        # chain_b returns, chain_a continues to fallback
        assert result.action.label == "Cortex/ChainAFallback"
        assert result.chain == "chain_a"

    def test_return_to_parent_continues_matching(self) -> None:
        """Test that return allows parent chain to continue matching."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="sender@example.com",
            to_addrs=["me@example.com"],
            subject="Test email",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="sub_chain"),
                    # This should be evaluated after sub_chain returns
                    Rule(
                        match=MatchCondition(from_contains="sender"),
                        action=Action(label="MatchedAfterReturn"),
                    ),
                    Rule(match=MatchCondition(), action=Action(label="Fallback")),
                ],
                "sub_chain": [
                    Rule(match=MatchCondition(), return_to_parent=True),
                ],
            }
        )

        result = evaluate(config, email)

        assert result.matched
        assert result.action is not None
        # Should match the rule with from_contains="sender" after return
        assert result.action.label == "Cortex/MatchedAfterReturn"

    def test_return_to_parent_no_subsequent_match(self) -> None:
        """Test return_to_parent when no subsequent rules match in parent."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="unknown@example.com",
            to_addrs=["me@example.com"],
            subject="Unknown email",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="sub_chain"),
                    # This won't match
                    Rule(
                        match=MatchCondition(from_contains="specific"),
                        action=Action(label="Specific"),
                    ),
                ],
                "sub_chain": [
                    Rule(match=MatchCondition(), return_to_parent=True),
                ],
            }
        )

        result = evaluate(config, email)

        # No rules matched after return
        assert not result.matched
        assert result.action is None

    def test_return_to_parent_in_main_chain(self) -> None:
        """Test return_to_parent in main chain (edge case - no parent)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        config = RulesConfig(
            chains={
                "main": [
                    # return_to_parent in main chain - should behave as no match
                    Rule(match=MatchCondition(), return_to_parent=True),
                    Rule(match=MatchCondition(), action=Action(label="Fallback")),
                ],
            }
        )

        result = evaluate(config, email)

        # return_to_parent in main chain means no match (nowhere to return to)
        assert not result.matched
        assert result.returned is True

    def test_return_to_parent_trace_includes_chain_name(self) -> None:
        """Test that return_to_parent trace message includes chain name."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
        )
        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="sub_chain"),
                ],
                "sub_chain": [
                    Rule(match=MatchCondition(), return_to_parent=True),
                ],
            }
        )

        result = evaluate(config, email)

        # Check trace includes chain name
        trace_str = " ".join(result.trace)
        assert "sub_chain" in trace_str
        assert "returning to parent" in trace_str

    def test_return_to_parent_conditional(self) -> None:
        """Test return_to_parent with conditional match."""
        # Email that doesn't match return condition
        email1 = Email(
            id=1,
            gmail_id="abc123",
            from_addr="receipt@store.com",
            to_addrs=["me@example.com"],
            subject="Your receipt",
        )
        # Email that matches return condition
        email2 = Email(
            id=2,
            gmail_id="def456",
            from_addr="updates@kickstarter.com",
            to_addrs=["me@example.com"],
            subject="Project Update",
        )

        config = RulesConfig(
            chains={
                "main": [
                    Rule(match=MatchCondition(), jump="transactional"),
                    Rule(
                        match=MatchCondition(), action=Action(label="NonTransactional")
                    ),
                ],
                "transactional": [
                    # Only kickstarter updates return
                    Rule(
                        match=MatchCondition(from_contains="kickstarter"),
                        return_to_parent=True,
                    ),
                    Rule(match=MatchCondition(), action=Action(label="Transactional")),
                ],
            }
        )

        # Email1 should stay in transactional chain
        result1 = evaluate(config, email1)
        assert result1.matched
        assert result1.action is not None
        assert result1.action.label == "Cortex/Transactional"
        assert result1.chain == "transactional"

        # Email2 should return and match NonTransactional
        result2 = evaluate(config, email2)
        assert result2.matched
        assert result2.action is not None
        assert result2.action.label == "Cortex/NonTransactional"
        assert result2.chain == "main"


class TestHeaderMatching:
    """Tests for header matching conditions."""

    def test_header_glob_match(self) -> None:
        """Test header matching with glob pattern."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="list@googlegroups.com",
            to_addrs=["me@example.com"],
            subject="[nyc-resistor] Meeting tonight",
            headers={"list-id": "<nycresistor.googlegroups.com>"},
        )

        # Match with glob pattern
        condition = MatchCondition(header={"List-ID": "*googlegroups*"})
        assert matches_condition(condition, email)

        # No match with different pattern
        condition = MatchCondition(header={"List-ID": "*yahoogroups*"})
        assert not matches_condition(condition, email)

    def test_header_glob_case_insensitive(self) -> None:
        """Test header matching is case-insensitive for header names."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={"reply-to": "noreply@example.com"},
        )

        # Header name case shouldn't matter
        condition = MatchCondition(header={"Reply-To": "*noreply*"})
        assert matches_condition(condition, email)

        condition = MatchCondition(header={"REPLY-TO": "*noreply*"})
        assert matches_condition(condition, email)

    def test_header_contains_match(self) -> None:
        """Test header_contains for substring matching."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="list@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={"list-id": "<nycresistor.googlegroups.com>"},
        )

        # Match substring
        condition = MatchCondition(header_contains={"List-ID": "nycresistor"})
        assert matches_condition(condition, email)

        # No match
        condition = MatchCondition(header_contains={"List-ID": "hackathon"})
        assert not matches_condition(condition, email)

    def test_header_missing_returns_false(self) -> None:
        """Test that missing header returns False."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={},  # No headers
        )

        condition = MatchCondition(header={"List-ID": "*"})
        assert not matches_condition(condition, email)

    def test_multiple_headers_all_must_match(self) -> None:
        """Test that all specified headers must match (AND)."""
        email = Email(
            id=1,
            gmail_id="abc123",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={
                "list-id": "<test.googlegroups.com>",
                "reply-to": "noreply@test.com",
            },
        )

        # Both match
        condition = MatchCondition(
            header={"List-ID": "*googlegroups*", "Reply-To": "*noreply*"}
        )
        assert matches_condition(condition, email)

        # One fails
        condition = MatchCondition(
            header={"List-ID": "*googlegroups*", "Reply-To": "*support*"}
        )
        assert not matches_condition(condition, email)
