"""Tests for Email Similarity Engine."""

from decimal import Decimal

from triage.services.similarity import (
    extract_merchant_from_subject,
    group_by_similarity,
    match_subject,
)


class TestMatchSubject:
    """Tests for subject pattern matching."""

    def test_paypal_receipt_usd(self) -> None:
        """Test PayPal receipt with USD."""
        result = match_subject("Host Havoc Ltd.: $10.00 USD")
        assert result is not None
        assert result.template_name == "paypal_receipt"
        assert result.merchant == "Host Havoc Ltd."
        assert result.amount is not None
        assert result.amount.amount == Decimal("10.00")
        assert result.amount.currency == "USD"

    def test_paypal_receipt_eur(self) -> None:
        """Test PayPal receipt with EUR."""
        result = match_subject("Mango 3D: €9.99 EUR")
        assert result is not None
        assert result.template_name == "paypal_receipt"
        assert result.merchant == "Mango 3D"
        assert result.amount is not None
        assert result.amount.amount == Decimal("9.99")
        assert result.amount.currency == "EUR"

    def test_paypal_receipt_truncated_merchant(self) -> None:
        """Test PayPal receipt with truncated merchant name."""
        result = match_subject("Bright Market LLC db...: $10.50 USD")
        assert result is not None
        assert result.template_name == "paypal_receipt"
        # Should clean up the trailing dots
        assert result.merchant == "Bright Market LLC db"
        assert result.amount is not None
        assert result.amount.amount == Decimal("10.50")

    def test_paypal_receipt_large_amount(self) -> None:
        """Test PayPal receipt with large amount."""
        result = match_subject("The Home Depot: $216.40 USD")
        assert result is not None
        assert result.merchant == "The Home Depot"
        assert result.amount is not None
        assert result.amount.amount == Decimal("216.40")

    def test_paypal_auto_payment(self) -> None:
        """Test PayPal automatic payment notification."""
        result = match_subject(
            "You sent an automatic payment to Wikimedia Foundation, Inc."
        )
        assert result is not None
        assert result.template_name == "paypal_auto_payment"
        # Trailing period is stripped (sentence punctuation)
        assert result.merchant == "Wikimedia Foundation, Inc"
        assert result.amount is None

    def test_paypal_auto_payment_truncated(self) -> None:
        """Test PayPal automatic payment with truncated name."""
        result = match_subject("You sent an automatic payment to Wikimedia ...")
        assert result is not None
        assert result.template_name == "paypal_auto_payment"
        assert result.merchant == "Wikimedia"

    def test_apple_receipt(self) -> None:
        """Test Apple receipt."""
        result = match_subject("Your receipt from Apple.")
        assert result is not None
        assert result.template_name == "apple_receipt"
        # Merchant is always "Apple" for this template (set in match_subject)
        assert result.merchant == "Apple"

    def test_order_shipped(self) -> None:
        """Test order shipped notification."""
        result = match_subject("Your AMERICAN BUTTON M order is on its way")
        assert result is not None
        assert result.template_name == "order_shipped"
        assert result.merchant == "AMERICAN BUTTON M"

    def test_recurring_reactivated(self) -> None:
        """Test recurring payment reactivated status."""
        result = match_subject("Recurring Payment Reactivated")
        assert result is not None
        assert result.template_name == "recurring_reactivated"
        assert result.merchant is None

    def test_amount_changing(self) -> None:
        """Test automatic payment amount changing."""
        result = match_subject("Your automatic payment amount is changing")
        assert result is not None
        assert result.template_name == "amount_changing"

    def test_status_changed(self) -> None:
        """Test automatic payment status changed."""
        result = match_subject("Your automatic payment status has changed")
        assert result is not None
        assert result.template_name == "status_changed"

    def test_payment_authorized(self) -> None:
        """Test payment authorized."""
        result = match_subject("You authorized a payment")
        assert result is not None
        assert result.template_name == "payment_authorized"

    def test_unknown_pattern_returns_none(self) -> None:
        """Test that unknown patterns return None."""
        result = match_subject("Random email subject that doesn't match")
        assert result is None

    def test_paypal_receipt_no_cents(self) -> None:
        """Test PayPal receipt without cents."""
        result = match_subject("Some Merchant: $50 USD")
        assert result is not None
        assert result.amount is not None
        assert result.amount.amount == Decimal("50")


class TestExtractMerchantFromSubject:
    """Tests for the simplified merchant extraction function."""

    def test_paypal_receipt(self) -> None:
        """Test merchant extraction from PayPal receipt."""
        merchant, amount = extract_merchant_from_subject("Host Havoc Ltd.: $10.00 USD")
        assert merchant == "Host Havoc Ltd."
        assert amount is not None
        assert amount.amount == Decimal("10.00")
        assert amount.currency == "USD"

    def test_apple_receipt(self) -> None:
        """Test merchant extraction from Apple receipt."""
        merchant, amount = extract_merchant_from_subject("Your receipt from Apple.")
        assert merchant == "Apple"
        assert amount is None

    def test_auto_payment(self) -> None:
        """Test merchant extraction from auto-payment."""
        merchant, amount = extract_merchant_from_subject(
            "You sent an automatic payment to Netflix Inc."
        )
        # Trailing period is stripped (sentence punctuation)
        assert merchant == "Netflix Inc"
        assert amount is None

    def test_no_match(self) -> None:
        """Test no extraction when pattern doesn't match."""
        merchant, amount = extract_merchant_from_subject("Hello world")
        assert merchant is None
        assert amount is None


class TestGroupBySimilarity:
    """Tests for grouping emails by similarity."""

    def test_group_same_merchant(self) -> None:
        """Test that emails with same merchant are grouped."""
        emails = [
            {
                "gmail_id": "a1",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-12-19",
            },
            {
                "gmail_id": "a2",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-11-19",
            },
            {
                "gmail_id": "a3",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-10-19",
            },
        ]
        groups = group_by_similarity(emails, "service@paypal.com")

        assert len(groups) == 1
        group = groups[0]
        assert group.merchant == "Host Havoc Ltd."
        assert group.template_name == "paypal_receipt"
        assert len(group.gmail_ids) == 3
        assert len(group.amounts) == 3
        assert all(a.amount == Decimal("10.00") for a in group.amounts)

    def test_group_different_merchants(self) -> None:
        """Test that different merchants create different groups."""
        emails = [
            {
                "gmail_id": "a1",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-12-19",
            },
            {
                "gmail_id": "a2",
                "subject": "Bright Market LLC: $10.50 USD",
                "date_header": "2025-12-13",
            },
            {
                "gmail_id": "a3",
                "subject": "Apple Services: $2.99 USD",
                "date_header": "2025-12-14",
            },
        ]
        groups = group_by_similarity(emails, "service@paypal.com")

        assert len(groups) == 3
        merchants = {g.merchant for g in groups}
        assert "Host Havoc Ltd." in merchants
        assert "Bright Market LLC" in merchants
        assert "Apple Services" in merchants

    def test_group_mixed_patterns(self) -> None:
        """Test grouping with different pattern types."""
        emails = [
            {
                "gmail_id": "a1",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-12-19",
            },
            {
                "gmail_id": "a2",
                "subject": "Recurring Payment Reactivated",
                "date_header": "2025-11-20",
            },
            {
                "gmail_id": "a3",
                "subject": "You authorized a payment",
                "date_header": "2025-11-10",
            },
        ]
        groups = group_by_similarity(emails, "service@paypal.com")

        assert len(groups) == 3
        template_names = {g.template_name for g in groups}
        assert "paypal_receipt" in template_names
        assert "recurring_reactivated" in template_names
        assert "payment_authorized" in template_names

    def test_group_varying_amounts(self) -> None:
        """Test that varying amounts are captured in the group."""
        emails = [
            {
                "gmail_id": "a1",
                "subject": "Apple Services: $2.99 USD",
                "date_header": "2025-12-14",
            },
            {
                "gmail_id": "a2",
                "subject": "Apple Services: $7.35 USD",
                "date_header": "2025-12-13",
            },
            {
                "gmail_id": "a3",
                "subject": "Apple Services: $5.99 USD",
                "date_header": "2025-12-08",
            },
        ]
        groups = group_by_similarity(emails, "service@paypal.com")

        assert len(groups) == 1
        group = groups[0]
        assert group.merchant == "Apple Services"
        assert len(group.amounts) == 3
        amounts = [a.amount for a in group.amounts]
        assert Decimal("2.99") in amounts
        assert Decimal("7.35") in amounts
        assert Decimal("5.99") in amounts

    def test_group_unknown_patterns(self) -> None:
        """Test that unknown patterns are grouped separately."""
        emails = [
            {
                "gmail_id": "a1",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-12-19",
            },
            {
                "gmail_id": "a2",
                "subject": "Random unknown subject",
                "date_header": "2025-12-10",
            },
            {
                "gmail_id": "a3",
                "subject": "Another random subject",
                "date_header": "2025-12-05",
            },
        ]
        groups = group_by_similarity(emails, "service@paypal.com")

        # Should have paypal_receipt group and unknown group
        assert len(groups) == 2
        unknown_groups = [g for g in groups if g.template_name == "unknown"]
        assert len(unknown_groups) == 1
        assert len(unknown_groups[0].gmail_ids) == 2


class TestRealWorldExamples:
    """Tests using real-world PayPal email examples."""

    def test_full_paypal_analysis(self) -> None:
        """Test analyzing a batch of real PayPal subjects."""
        emails = [
            {
                "gmail_id": "1",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-12-19",
            },
            {
                "gmail_id": "2",
                "subject": "Launch Pad Payment S...: $15.00 USD",
                "date_header": "2025-12-16",
            },
            {
                "gmail_id": "3",
                "subject": "Apple Services: $2.99 USD",
                "date_header": "2025-12-14",
            },
            {
                "gmail_id": "4",
                "subject": "Apple Services: $7.35 USD",
                "date_header": "2025-12-13",
            },
            {
                "gmail_id": "5",
                "subject": "Bright Market LLC db...: $10.50 USD",
                "date_header": "2025-12-13",
            },
            {
                "gmail_id": "6",
                "subject": "Disney Plus: $36.01 USD",
                "date_header": "2025-12-02",
            },
            {
                "gmail_id": "7",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-11-19",
            },
            {
                "gmail_id": "8",
                "subject": "Bright Market LLC db...: $10.50 USD",
                "date_header": "2025-11-13",
            },
            {
                "gmail_id": "9",
                "subject": "Host Havoc Ltd.: $10.00 USD",
                "date_header": "2025-10-19",
            },
            {
                "gmail_id": "10",
                "subject": "Bright Market LLC db...: $10.50 USD",
                "date_header": "2025-10-14",
            },
        ]

        groups = group_by_similarity(emails, "service@paypal.com")

        # Should have separate groups for each merchant
        merchants = {g.merchant: g for g in groups}

        # Host Havoc: 3 occurrences
        assert "Host Havoc Ltd." in merchants
        assert len(merchants["Host Havoc Ltd."].gmail_ids) == 3

        # Bright Market: 3 occurrences (truncated in subject)
        bright_market = [
            g for g in groups if g.merchant and "Bright Market" in g.merchant
        ]
        assert len(bright_market) == 1
        assert len(bright_market[0].gmail_ids) == 3

        # Apple Services: 2 occurrences
        assert "Apple Services" in merchants
        assert len(merchants["Apple Services"].gmail_ids) == 2

        # Disney Plus: 1 occurrence
        assert "Disney Plus" in merchants
        assert len(merchants["Disney Plus"].gmail_ids) == 1

        # Launch Pad: 1 occurrence
        launch_pad = [g for g in groups if g.merchant and "Launch Pad" in g.merchant]
        assert len(launch_pad) == 1
