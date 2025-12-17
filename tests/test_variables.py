"""Tests for variable resolution in dynamic labels."""

import pytest

from triage.engine.variables import (
    VariableContext,
    VariableError,
    resolve_header_regex,
    resolve_subject_regex,
    resolve_variables,
)
from triage.models.rules import (
    Email,
    HeaderRegexVariable,
    LLMVariable,
    SubjectRegexVariable,
    Variable,
)


@pytest.fixture
def github_email() -> Email:
    """Sample GitHub email with List-ID header."""
    return Email(
        id=1,
        gmail_id="github123",
        from_addr="notifications@github.com",
        to_addrs=["me@example.com"],
        subject="[devonjones/postmark] Fix email parsing bug (#42)",
        body="A fix has been merged for the email parsing bug.",
        has_attachment=False,
        headers={
            "list-id": "devonjones/postmark <postmark.devonjones.github.com>",
            "x-github-sender": "contributor",
        },
    )


@pytest.fixture
def amazon_email() -> Email:
    """Sample Amazon order email."""
    return Email(
        id=2,
        gmail_id="amazon456",
        from_addr="auto-confirm@amazon.com",
        to_addrs=["me@example.com"],
        subject="Your Amazon.com order #111-2345678-9012345 has shipped",
        body="Track your package at...",
        has_attachment=False,
        headers={},
    )


@pytest.fixture
def kickstarter_email() -> Email:
    """Sample Kickstarter project email."""
    return Email(
        id=3,
        gmail_id="kickstarter789",
        from_addr="no-reply@kickstarter.com",
        to_addrs=["me@example.com"],
        subject="Update #5 from Cool Widget Pro",
        body="Great news backers! Cool Widget Pro is shipping next month.",
        has_attachment=False,
        headers={},
    )


class TestVariableContext:
    """Tests for VariableContext dataclass."""

    def test_empty_context(self) -> None:
        """Test empty context returns None for unknown keys."""
        ctx = VariableContext()
        assert ctx.get("unknown") is None

    def test_context_get(self) -> None:
        """Test context.get returns stored values."""
        ctx = VariableContext(values={"repo": "devonjones/postmark"})
        assert ctx.get("repo") == "devonjones/postmark"
        assert ctx.get("unknown") is None


class TestResolveHeaderRegex:
    """Tests for header regex extraction."""

    def test_github_list_id_extraction(self, github_email: Email) -> None:
        """Test extracting repo name from GitHub List-ID header."""
        config = HeaderRegexVariable(
            header="list-id",
            pattern=r"^([^\s<]+)",  # Captures "devonjones/postmark"
            group=1,
        )
        result = resolve_header_regex(github_email, config)
        assert result == "devonjones/postmark"

    def test_header_case_insensitive(self, github_email: Email) -> None:
        """Test header name matching is case-insensitive."""
        config = HeaderRegexVariable(
            header="List-ID",  # Different case
            pattern=r"^([^\s<]+)",
            group=1,
        )
        result = resolve_header_regex(github_email, config)
        assert result == "devonjones/postmark"

    def test_header_missing_returns_none(self, amazon_email: Email) -> None:
        """Test missing header returns None."""
        config = HeaderRegexVariable(
            header="list-id",
            pattern=r"^([^\s<]+)",
            group=1,
        )
        result = resolve_header_regex(amazon_email, config)
        assert result is None

    def test_regex_no_match_returns_none(self, github_email: Email) -> None:
        """Test regex that doesn't match returns None."""
        config = HeaderRegexVariable(
            header="list-id",
            pattern=r"NOMATCH(\d+)",  # Won't match
            group=1,
        )
        result = resolve_header_regex(github_email, config)
        assert result is None

    def test_custom_capture_group(self, github_email: Email) -> None:
        """Test extracting different capture groups."""
        # Pattern with two groups
        config = HeaderRegexVariable(
            header="list-id",
            pattern=r"([^/]+)/([^\s<]+)",  # (owner)/(repo)
            group=2,  # Get repo only
        )
        result = resolve_header_regex(github_email, config)
        assert result == "postmark"

    def test_invalid_regex_returns_none(self, github_email: Email) -> None:
        """Test invalid regex pattern returns None."""
        config = HeaderRegexVariable(
            header="list-id",
            pattern=r"[invalid(",  # Invalid regex
            group=1,
        )
        result = resolve_header_regex(github_email, config)
        assert result is None


class TestResolveSubjectRegex:
    """Tests for subject regex extraction."""

    def test_amazon_order_id_extraction(self, amazon_email: Email) -> None:
        """Test extracting Amazon order ID from subject."""
        config = SubjectRegexVariable(
            pattern=r"#(\d{3}-\d{7}-\d{7})",
            group=1,
        )
        result = resolve_subject_regex(amazon_email, config)
        assert result == "111-2345678-9012345"

    def test_github_pr_number_extraction(self, github_email: Email) -> None:
        """Test extracting PR number from GitHub subject."""
        config = SubjectRegexVariable(
            pattern=r"#(\d+)",  # Match issue/PR number
            group=1,
        )
        result = resolve_subject_regex(github_email, config)
        assert result == "42"

    def test_subject_regex_no_match(self, kickstarter_email: Email) -> None:
        """Test regex that doesn't match subject returns None."""
        config = SubjectRegexVariable(
            pattern=r"#(\d{3}-\d{7}-\d{7})",  # Amazon order format
            group=1,
        )
        result = resolve_subject_regex(kickstarter_email, config)
        assert result is None

    def test_subject_regex_case_insensitive(self, amazon_email: Email) -> None:
        """Test subject regex matching is case-insensitive."""
        config = SubjectRegexVariable(
            pattern=r"AMAZON\.COM order #(\d{3}-\d{7}-\d{7})",
            group=1,
        )
        result = resolve_subject_regex(amazon_email, config)
        assert result == "111-2345678-9012345"

    def test_invalid_subject_regex_returns_none(self, amazon_email: Email) -> None:
        """Test invalid regex pattern returns None."""
        config = SubjectRegexVariable(
            pattern=r"[invalid(",
            group=1,
        )
        result = resolve_subject_regex(amazon_email, config)
        assert result is None


class MockExtractionLLMClient:
    """Mock LLM client for testing variable extraction."""

    def __init__(
        self,
        extract_responses: dict[str, str | None] | None = None,
        classify_with_extraction_response: (
            tuple[str, float, str, dict[str, str]] | None
        ) = None,
    ):
        self.extract_responses = extract_responses or {}
        self.classify_with_extraction_response = classify_with_extraction_response or (
            "unknown",
            0.5,
            "",
            {},
        )
        self.extract_calls: list[tuple[str, str, str | None, str, str]] = []
        self.classify_with_extraction_calls: list[tuple[str, str, list[str] | None]] = (
            []
        )

    def extract_value(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> str | None:
        """Mock extract_value for testing."""
        self.extract_calls.append((from_addr, subject, body, prompt, model))
        # Look for keywords in prompt to determine response
        for keyword, response in self.extract_responses.items():
            if keyword.lower() in prompt.lower():
                return response
        return None

    def classify_with_extraction(
        self,
        prompt: str,
        model: str,
        extract_fields: list[str] | None = None,
    ) -> tuple[str, float, str, dict[str, str]]:
        """Mock classify_with_extraction for testing."""
        self.classify_with_extraction_calls.append((prompt, model, extract_fields))
        return self.classify_with_extraction_response


class TestResolveLLM:
    """Tests for LLM-based variable extraction."""

    def test_llm_extraction_success(self, kickstarter_email: Email) -> None:
        """Test successful LLM extraction."""
        from triage.engine.variables import resolve_llm

        config = LLMVariable(
            prompt="Extract the project name. Return ONLY the name.",
            model="test-model",
        )
        llm_client = MockExtractionLLMClient(
            extract_responses={"project name": "Cool Widget Pro"}
        )

        result = resolve_llm(kickstarter_email, config, llm_client)

        assert result == "Cool Widget Pro"
        assert len(llm_client.extract_calls) == 1
        # Verify the call was made with correct parameters
        call = llm_client.extract_calls[0]
        assert call[0] == "no-reply@kickstarter.com"  # from_addr
        assert call[1] == "Update #5 from Cool Widget Pro"  # subject
        assert call[4] == "test-model"  # model

    def test_llm_extraction_returns_none(self, kickstarter_email: Email) -> None:
        """Test LLM extraction that returns None."""
        from triage.engine.variables import resolve_llm

        config = LLMVariable(
            prompt="Extract something that doesn't exist",
            model="test-model",
        )
        llm_client = MockExtractionLLMClient(extract_responses={})

        result = resolve_llm(kickstarter_email, config, llm_client)

        assert result is None


class TestResolveVariables:
    """Tests for resolving all variables in a rule."""

    def test_resolve_header_regex_variable(self, github_email: Email) -> None:
        """Test resolving a header regex variable."""
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",
                    pattern=r"^([^\s<]+)",
                    group=1,
                )
            )
        }

        ctx = resolve_variables(github_email, variables)

        assert ctx.values == {"repo": "devonjones/postmark"}

    def test_resolve_subject_regex_variable(self, amazon_email: Email) -> None:
        """Test resolving a subject regex variable."""
        variables = {
            "order_id": Variable(
                subject_regex=SubjectRegexVariable(
                    pattern=r"#(\d{3}-\d{7}-\d{7})",
                    group=1,
                )
            )
        }

        ctx = resolve_variables(amazon_email, variables)

        assert ctx.values == {"order_id": "111-2345678-9012345"}

    def test_resolve_llm_variable(self, kickstarter_email: Email) -> None:
        """Test resolving an LLM variable."""
        variables = {
            "project": Variable(
                llm=LLMVariable(
                    prompt="Extract project name",
                    model="test-model",
                )
            )
        }
        llm_client = MockExtractionLLMClient(
            extract_responses={"project name": "Cool Widget Pro"}
        )

        ctx = resolve_variables(kickstarter_email, variables, llm_client)

        assert ctx.values == {"project": "Cool Widget Pro"}

    def test_resolve_multiple_variables(self, github_email: Email) -> None:
        """Test resolving multiple variables in one rule."""
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",
                    pattern=r"^([^\s<]+)",
                    group=1,
                )
            ),
            "pr_number": Variable(
                subject_regex=SubjectRegexVariable(
                    pattern=r"#(\d+)",
                    group=1,
                )
            ),
        }

        ctx = resolve_variables(github_email, variables)

        assert ctx.values["repo"] == "devonjones/postmark"
        assert ctx.values["pr_number"] == "42"

    def test_variable_failure_raises_error(self, amazon_email: Email) -> None:
        """Test that failed variable resolution raises VariableError."""
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",  # Amazon email doesn't have this
                    pattern=r"^([^\s<]+)",
                    group=1,
                )
            )
        }

        with pytest.raises(VariableError, match="repo"):
            resolve_variables(amazon_email, variables)

    def test_llm_variable_without_client_raises_error(
        self, kickstarter_email: Email
    ) -> None:
        """Test LLM variable without client raises VariableError."""
        variables = {
            "project": Variable(
                llm=LLMVariable(
                    prompt="Extract project name",
                    model="test-model",
                )
            )
        }

        with pytest.raises(VariableError, match="LLM client"):
            resolve_variables(kickstarter_email, variables, llm_client=None)

    def test_empty_value_raises_error(self, github_email: Email) -> None:
        """Test that empty extracted value raises VariableError."""
        # Patch the email to have empty header value
        email = Email(
            id=1,
            gmail_id="test",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={"list-id": ""},  # Empty value
        )
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",
                    pattern=r"(.*)",  # Would match empty string
                    group=1,
                )
            )
        }

        with pytest.raises(VariableError, match="repo"):
            resolve_variables(email, variables)

    def test_whitespace_only_value_raises_error(self, github_email: Email) -> None:
        """Test that whitespace-only extracted value raises VariableError."""
        email = Email(
            id=1,
            gmail_id="test",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={"list-id": "   "},  # Whitespace only
        )
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",
                    pattern=r"(.*)",
                    group=1,
                )
            )
        }

        with pytest.raises(VariableError, match="repo"):
            resolve_variables(email, variables)

    def test_values_are_stripped(self, github_email: Email) -> None:
        """Test that extracted values are stripped of whitespace."""
        email = Email(
            id=1,
            gmail_id="test",
            from_addr="test@example.com",
            to_addrs=["me@example.com"],
            subject="Test",
            headers={"list-id": "  owner/repo  "},  # Whitespace padding
        )
        variables = {
            "repo": Variable(
                header_regex=HeaderRegexVariable(
                    header="list-id",
                    pattern=r"^\s*([^\s]+/[^\s]+)\s*$",
                    group=1,
                )
            )
        }

        ctx = resolve_variables(email, variables)

        assert ctx.values["repo"] == "owner/repo"


class TestVariableModel:
    """Tests for Variable pydantic model validation."""

    def test_exactly_one_source_required(self) -> None:
        """Test that exactly one source type is required."""
        # No source - should fail
        with pytest.raises(ValueError, match="exactly one"):
            Variable()

    def test_multiple_sources_rejected(self) -> None:
        """Test that multiple source types are rejected."""
        with pytest.raises(ValueError, match="exactly one"):
            Variable(
                header_regex=HeaderRegexVariable(
                    header="test",
                    pattern=r"(.*)",
                ),
                subject_regex=SubjectRegexVariable(
                    pattern=r"(.*)",
                ),
            )

    def test_single_header_regex_valid(self) -> None:
        """Test that single header_regex source is valid."""
        var = Variable(
            header_regex=HeaderRegexVariable(
                header="test",
                pattern=r"(.*)",
            )
        )
        assert var.header_regex is not None
        assert var.subject_regex is None
        assert var.llm is None

    def test_single_subject_regex_valid(self) -> None:
        """Test that single subject_regex source is valid."""
        var = Variable(
            subject_regex=SubjectRegexVariable(
                pattern=r"(.*)",
            )
        )
        assert var.header_regex is None
        assert var.subject_regex is not None
        assert var.llm is None

    def test_single_llm_valid(self) -> None:
        """Test that single llm source is valid."""
        var = Variable(
            llm=LLMVariable(
                prompt="Extract something",
                model="test-model",
            )
        )
        assert var.header_regex is None
        assert var.subject_regex is None
        assert var.llm is not None
