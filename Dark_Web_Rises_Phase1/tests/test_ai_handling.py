import sys
import os
from unittest.mock import MagicMock

# Mock heavy/problematic dependencies
sys.modules['torch'] = MagicMock()
sys.modules['open_clip'] = MagicMock()
sys.modules['PIL'] = MagicMock()

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.ai_handling import classify_prompt, clamp_score, MAX_PROMPT_LENGTH

def test_classify_prompt_empty():
    assert classify_prompt("") is False
    assert classify_prompt("   ") is False
    assert classify_prompt(None) is False

def test_classify_prompt_too_long():
    long_prompt = "a " * (MAX_PROMPT_LENGTH // 2 + 1)
    assert classify_prompt(long_prompt) is False

def test_classify_prompt_too_short():
    assert classify_prompt("a simple prompt") is False  # only 3 words
    assert classify_prompt("hello world") is False

def test_classify_prompt_not_enough_letters():
    assert classify_prompt("a 123 456 789 000") is False

def test_classify_prompt_repeating_chars():
    assert classify_prompt("a simpleeeeee prooompt that is long enough") is False

def test_classify_prompt_no_common_words():
    # 4+ words, >60% letters, no 5x repeats, but no common words
    assert classify_prompt("exquisite magnificent resplendent idiosyncratic") is False

def test_classify_prompt_valid():
    # A valid prompt that meets all criteria
    assert classify_prompt("a simple prompt that is long enough to pass") is True
    # Another valid prompt
    assert classify_prompt("the quick brown fox jumps over the lazy dog") is True

def test_clamp_score():
    assert clamp_score(50.5) == 50.5
    assert clamp_score(-10.0) == 0.0
    assert clamp_score(150.0) == 100.0
    assert clamp_score(0.0) == 0.0
    assert clamp_score(100.0) == 100.0
    assert clamp_score(33.333) == 33.33

if __name__ == "__main__":
    test_classify_prompt_empty()
    test_classify_prompt_too_long()
    test_classify_prompt_too_short()
    test_classify_prompt_not_enough_letters()
    test_classify_prompt_repeating_chars()
    test_classify_prompt_no_common_words()
    test_classify_prompt_valid()
    test_clamp_score()
    print("All test_ai_handling tests passed!")
