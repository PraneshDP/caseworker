"""Tests for household age determination and Section 3.9 safeguarding rules."""

from datetime import date
import pytest
from src.domain.referral import HouseholdMember, ResidentHistory


class TestHouseholdMemberAge:
    """Test age calculation on HouseholdMember."""

    def test_child_under_18(self):
        # As of 2026-03-17, DOB 2021-02-26 is 5 years old
        m = HouseholdMember(name="Child A", date_of_birth="2021-02-26", relationship="Son/daughter")
        assert m.age_as_of(date(2026, 3, 17)) == 5
        assert m.is_under_18(date(2026, 3, 17)) is True

    def test_adult_over_18(self):
        # As of 2026-03-17, DOB 1964-05-25 is 61 years old
        m = HouseholdMember(name="Adult A", date_of_birth="1964-05-25", relationship="Applicant")
        assert m.age_as_of(date(2026, 3, 17)) == 61
        assert m.is_under_18(date(2026, 3, 17)) is False

    def test_boundary_18th_birthday(self):
        # Exactly 18 on 2026-03-17 -> DOB 2008-03-17
        m_exact = HouseholdMember(name="Bday", date_of_birth="2008-03-17")
        assert m_exact.age_as_of(date(2026, 3, 17)) == 18
        assert m_exact.is_under_18(date(2026, 3, 17)) is False

        # Turns 18 tomorrow (2008-03-18) -> age 17 on 2026-03-17 -> is under 18
        m_minor = HouseholdMember(name="Minor", date_of_birth="2008-03-18")
        assert m_minor.age_as_of(date(2026, 3, 17)) == 17
        assert m_minor.is_under_18(date(2026, 3, 17)) is True

    def test_missing_dob_returns_none(self):
        m = HouseholdMember(name="No DOB", date_of_birth="")
        assert m.age_as_of() is None
        assert m.is_under_18() is None


class TestResidentHistorySafeguardingCheck:
    """Test ResidentHistory.applies_section_3_9()."""

    def test_applies_when_child_in_household(self):
        history = ResidentHistory(
            resident_ref="R-20500",
            available=True,
            household=[
                HouseholdMember("Parent", "1980-01-01", "Applicant"),
                HouseholdMember("Child", "2020-05-10", "Son/daughter"),
            ],
        )
        applies, reason = history.applies_section_3_9(date(2026, 3, 17))
        assert applies is True
        assert "under 18" in reason
        assert "Child" in reason

    def test_does_not_apply_when_adults_only(self):
        history = ResidentHistory(
            resident_ref="R-20507",
            available=True,
            household=[
                HouseholdMember("Adult 1", "1975-04-12", "Applicant"),
                HouseholdMember("Adult 2", "1978-09-20", "Partner"),
            ],
        )
        applies, reason = history.applies_section_3_9(date(2026, 3, 17))
        assert applies is False
        assert "confirmed" in reason.lower()

    def test_fails_closed_when_history_unavailable(self):
        """ACA-2026/2 s.5.2 & s.6.1: if history unavailable, 3.9 applies."""
        history = ResidentHistory.unavailable("R-MISSING", "HTTP 500 server down")
        applies, reason = history.applies_section_3_9()
        assert applies is True
        assert "cannot be established" in reason

    def test_fails_closed_when_household_empty(self):
        history = ResidentHistory(resident_ref="R-EMPTY", available=True, household=[])
        applies, reason = history.applies_section_3_9()
        assert applies is True
        assert "unrecorded" in reason or "cannot be established" in reason

    def test_fails_closed_when_dob_unconfirmed(self):
        history = ResidentHistory(
            resident_ref="R-UNKNOWN-DOB",
            available=True,
            household=[HouseholdMember("Unknown", date_of_birth="")],
        )
        applies, reason = history.applies_section_3_9()
        assert applies is True
        assert "unconfirmed" in reason
