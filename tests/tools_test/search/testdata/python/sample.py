"""Demo python file for grep tool integration tests."""
import os
import sys
from typing import List, Dict, Optional, Any

class DemoGreeter:
    """A simple greeter class."""
    def __init__(self, name: str):
        self.name = name
        self._history: List[str] = []

    def say(self) -> str:
        msg = f"hello, {self.name}"
        self._history.append(msg)
        return msg

    def get_history(self) -> List[str]:
        return self._history.copy()

class AdvancedProcessor:
    """A more complex class to test AST parsing."""
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.is_active = config.get("active", False)

    def process_data(self, data: List[int]) -> List[int]:
        if not self.is_active:
            return []
        return [x * 2 for x in data if x > 0]

    @staticmethod
    def validate_config(config: Dict[str, Any]) -> bool:
        return "active" in config

def py_target_function(value: int) -> int:
    """Unique symbol for grep integration testing."""
    # Adding some complexity inside the target function
    if value < 0:
        return 0
    
    temp_result = value * 2
    return temp_result + 1

def helper_function(items: List[str]) -> str:
    """Just another function to add noise."""
    return ", ".join(items)

if __name__ == "__main__":
    greeter = DemoGreeter("World")
    print(greeter.say())
    print(f"Target result: {py_target_function(5)}")
