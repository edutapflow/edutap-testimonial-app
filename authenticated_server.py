"""Streamlit's ASGI server wrapped in a password gate for HTTP and WebSockets."""
import os
from pathlib import Path

import streamlit as st
from streamlit import config
from auth_gate import PasswordGate

ROOT = Path(__file__).resolve().parent


def create_application():
    os.chdir(ROOT)
    config.set_option('server.headless', True)
    config.set_option('server.fileWatcherType', 'none')
    config.set_option('browser.gatherUsageStats', False)
    # Version is pinned because st.App is an experimental ASGI interface.
    streamlit_app = st.App(str(ROOT / 'app.py'))
    return PasswordGate(streamlit_app)
