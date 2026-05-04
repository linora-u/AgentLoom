import os


def get_value():
    return f"helper-ok:{os.path.basename(os.getcwd())}"
