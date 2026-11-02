import pytest

from museaigc.journal import Journal, JournalError


def test_reserve_once(tmp_path):
    j = Journal(str(tmp_path), "T")
    j.write("PREPARED"); j.write("SEND_RESERVED")
    with pytest.raises(JournalError):
        j.write("SEND_RESERVED")


def test_unknown_state(tmp_path):
    with pytest.raises(JournalError):
        Journal(str(tmp_path), "T").write("ART_PASS")
