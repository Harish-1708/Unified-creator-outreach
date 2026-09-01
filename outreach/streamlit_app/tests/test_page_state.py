import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
from page_state import mark_active_page


def _reset():
    if "_active_page" in st.session_state:
        del st.session_state["_active_page"]


def test_first_call_ever_counts_as_navigation():
    _reset()
    assert mark_active_page("campaigns") is True


def test_repeated_call_on_same_page_is_not_navigation():
    _reset()
    mark_active_page("campaigns")
    assert mark_active_page("campaigns") is False
    assert mark_active_page("campaigns") is False  # still False on further same-page reruns


def test_call_on_different_page_then_back_is_navigation():
    _reset()
    mark_active_page("campaigns")
    mark_active_page("controls")  # navigated away
    assert mark_active_page("campaigns") is True  # navigated back — this IS a fresh arrival


def test_marker_persists_correctly_across_many_page_switches():
    _reset()
    assert mark_active_page("a") is True
    assert mark_active_page("a") is False
    assert mark_active_page("b") is True
    assert mark_active_page("b") is False
    assert mark_active_page("a") is True
