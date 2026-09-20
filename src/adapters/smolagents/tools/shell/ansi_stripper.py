import re

# Regex to match 7-bit and 8-bit C1 ANSI sequences
# Matches standard ANSI color codes, text formatting, and movement sequences.
ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')
# Matches OSC (Operating System Command) sequences, typically ending in BEL (\x07)
osc_escape = re.compile(r'\x1B\][^\x07]*\x07')
# Matches basic VT100 escape sequences like Application Keypad (\x1b=) and Normal Keypad (\x1b>)
vt100_escape = re.compile(r'\x1B[=>]')

def strip_ansi(text: str) -> str:
    """Removes ANSI escape codes (e.g., color formatting) from a string and resolves backspaces."""
    if not isinstance(text, str):
        return text
    text = osc_escape.sub('', text)
    text = vt100_escape.sub('', text)
    text = ansi_escape.sub('', text)
    
    # Intelligently resolve backspaces (\x08)
    if "\x08" in text:
        result = []
        for char in text:
            if char == "\x08":
                if result:
                    result.pop()
            else:
                result.append(char)
        text = "".join(result)
        
    # Normalize carriage returns
    text = text.replace("\r\r\n", "\n").replace("\r\n", "\n")
    return text
