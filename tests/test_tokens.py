"""Tests for ds.tokens — token counting and packing."""
import pytest
from ds import tokens


class TestCount:
    def test_empty_string(self):
        assert tokens.count("") == 0

    def test_single_word(self):
        assert tokens.count("hello") > 0

    def test_longer_text_more_tokens(self):
        short = tokens.count("hello")
        long_ = tokens.count("hello world this is a longer sentence with more words")
        assert long_ > short

    def test_deterministic(self):
        t = "ADXL345 register POWER_CTL"
        assert tokens.count(t) == tokens.count(t)


class TestTruncateTo:
    def test_no_truncation_needed(self):
        text, was_trunc = tokens.truncate_to("short text", 100)
        assert text == "short text"
        assert was_trunc is False

    def test_truncation_applied(self):
        # create a long text that exceeds 5 tokens
        long_text = "one two three four five six seven eight nine ten"
        text, was_trunc = tokens.truncate_to(long_text, 3)
        assert was_trunc is True
        assert len(text) < len(long_text)

    def test_exact_limit(self):
        text = "hello world"
        n = tokens.count(text)
        result, was_trunc = tokens.truncate_to(text, n)
        assert result == text
        assert was_trunc is False

    def test_zero_budget(self):
        text, was_trunc = tokens.truncate_to("hello", 0)
        assert was_trunc is True


class TestPack:
    def test_all_fit(self):
        blocks = ["block one", "block two", "block three"]
        text, n = tokens.pack(blocks, budget=200)
        assert n == 3
        assert "block one" in text
        assert "block three" in text

    def test_budget_exceeded_stops_at_whole_block(self):
        # Make blocks that are individually ~3 tokens each
        blocks = ["word one two", "word three four", "word five six", "word seven eight"]
        text, n = tokens.pack(blocks, budget=5)
        assert n < len(blocks)
        # All included blocks must appear fully
        for block in blocks[:n]:
            assert block in text

    def test_empty_blocks(self):
        text, n = tokens.pack([], budget=100)
        assert text == ""
        assert n == 0

    def test_single_block_always_included(self):
        # Even if block exceeds budget, first block is always included
        block = "this is a block with several tokens in it"
        text, n = tokens.pack([block], budget=1)
        assert n == 1
        assert text == block

    def test_separator_applied(self):
        blocks = ["A", "B"]
        text, _ = tokens.pack(blocks, budget=200, sep="\n---\n")
        assert "\n---\n" in text

    def test_default_separator_double_newline(self):
        blocks = ["A", "B"]
        text, _ = tokens.pack(blocks, budget=200)
        assert "\n\n" in text
