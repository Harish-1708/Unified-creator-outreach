"""Tiny helper for telling apart a genuine page navigation from an
in-page widget rerun. Both look IDENTICAL from a single page's own
script — Streamlit re-executes the whole script either way — so telling
them apart needs a marker that EVERY page updates, not just the one that
cares about the distinction.

Why this exists: a page that shows a dialog/form conditionally on
st.session_state (needed so the dialog survives reruns triggered by its
own widgets — otherwise it closes the instant you type anything into it)
has no natural way to know "the user navigated away without finishing,
then came back" versus "still actively filling this out" — both are just
"the script ran again" from that page's own point of view. Without this,
a dialog opened once can silently reappear every time you return to the
page, even from a completely unrelated later visit.
"""
import streamlit as st


def mark_active_page(page_id: str) -> bool:
    """Call once, near the top of every page (including the home page).
    Returns True if the previous page rendered (in this browser session)
    was a DIFFERENT one — meaning this run is a genuine arrival at this
    page, not a rerun triggered while already on it."""
    previous = st.session_state.get("_active_page")
    st.session_state["_active_page"] = page_id
    return previous != page_id
