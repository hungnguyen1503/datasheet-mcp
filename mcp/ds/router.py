"""Query router for ds_auto — classify a query to the best backend.

Routes: "register", "bit", "operation", "pin", "spec", "order", "search".

Deterministic regex classification (no ML), mirroring HUM's _classify_query but
tuned for component-datasheet vocabulary.
"""

from __future__ import annotations

import re

_REG_RE = re.compile(r'(?<!\w)([A-Z][A-Z0-9_]{1,15})(?!\w)')

# Procedural keywords → operation
_OP_RE = re.compile(
    r'\b(?:how\s+to|configur\w*|initializ\w*|enabl\w*|disabl\w*|sequence|procedure|'
    r'flow|steps?|set\s*up|start[\s-]?up|power[\s-]?up|operating\s+mode|read\s+data|'
    r'write\s+sequence)\b', re.IGNORECASE)

# Pin-assignment keywords → pin
_PIN_RE = re.compile(
    r'\b(?:which\s+pin|what\s+pin|pin\s*out|pinout|pin\s+assign|pad|package\s+pin|'
    r'sda|scl|sclk|sdo|sdi|mosi|miso|sck|cs|chip\s+select|vdd|vddio|gnd)\b',
    re.IGNORECASE)

# Value/concept keywords — "tell me the value", not "show me the register"
_VALUE_RE = re.compile(
    r'\b(?:voltage|supply|current|frequency|temperature|package|dimension|'
    r'specification|spec|range|sensitivity|resolution|bandwidth|address|overview|'
    r'datasheet|absolute\s+maximum|operating\s+condition)\b', re.IGNORECASE)

# Spec/parameter keywords → search with content_type="spec"
_SPEC_RE = re.compile(
    r'\b(?:supply\s+voltage|operating\s+(?:current|voltage)|'
    r'power\s+consumption|current\s+consumption|'
    r'sensitivity|resolution|absolute\s+maximum|rating|'
    r'temperature\s+range|timing\s+(?:characteristic|diagram|parameter)|'
    r'output\s+(?:current|voltage|drive)|'
    r'input\s+(?:current|voltage|leakage)|'
    r'dc\s+characteristic|ac\s+characteristic|'
    r'(?:supply|operating)\s+(?:voltage|current|range))\b', re.IGNORECASE)

# Ordering/part-number keywords → search with content_type="order"
_ORDER_RE = re.compile(
    r'\b(?:ordering|part\s+number|part\s+code|package\s+code|'
    r'order\s+code|how\s+to\s+order|part\s+family|'
    r'available\s+(?:parts?|options?|grades?)|'
    r'temperature\s+(?:grade|range)|package\s+option|'
    r'part\s+marking|ordering\s+information|'
    r'package\s+(?:type|dimension|information))\b', re.IGNORECASE)

_BIT_CONTEXT_RE = re.compile(r'([A-Z][A-Z0-9_]{1,15})\s+(?:bit|flag|field)', re.IGNORECASE)

# Common uppercase acronyms that are NOT register names
_COMMON_WORDS: frozenset[str] = frozenset({
    'MCU', 'CPU', 'RAM', 'ROM', 'USB', 'CAN', 'DMA', 'ADC', 'DAC', 'PWM',
    'RTC', 'LCD', 'LED', 'GPIO', 'SPI', 'I2C', 'IIC', 'BUS', 'RX', 'TX',
    'OK', 'ID', 'IO', 'PC', 'SP', 'IP', 'FIFO', 'SOC', 'IC', 'LSB', 'MSB',
    'VDD', 'GND', 'POR', 'ESD', 'PHY', 'MAC', 'ETH',
})

# Pin/signal names that should not be treated as block names
_PIN_SIGNALS: frozenset[str] = frozenset({
    'SDA', 'SCL', 'SCLK', 'SDO', 'SDI', 'MOSI', 'MISO', 'SCK', 'CS', 'SS',
    'INT', 'IRQ', 'NMI', 'CLK', 'VDD', 'VDDIO', 'GND', 'RESET',
})


def extract_block(query: str) -> str | None:
    """Pull the first plausible block name from the query text."""
    for tok in _REG_RE.findall(query):
        if tok not in _COMMON_WORDS and tok not in _PIN_SIGNALS and len(tok) <= 12:
            return tok
    return None


def classify_query(query: str, block: str | None) -> tuple[str, dict]:
    """Return (route, kwargs) for the best backend to handle this query."""
    # 1. Procedural keywords → operation
    if _OP_RE.search(query):
        return "operation", {"block": block or extract_block(query)}

    # 2. Pin-assignment keywords → pin
    if _PIN_RE.search(query):
        return "pin", {"block": block}

    # 3. Spec/parameter keywords → spec search
    if _SPEC_RE.search(query):
        return "spec", {}

    # 4. Ordering/part-number keywords → order search
    if _ORDER_RE.search(query):
        return "order", {}

    # 5. ALLCAPS register-like token
    candidates = [t for t in _REG_RE.findall(query) if t not in _COMMON_WORDS]
    if candidates:
        if _VALUE_RE.search(query):
            return "search", {}
        bit_m = _BIT_CONTEXT_RE.search(query)
        if bit_m and len(candidates) >= 2:
            bit_tok = bit_m.group(1).upper()
            reg_tok = next((t for t in candidates if t != bit_tok), None)
            if reg_tok and bit_tok != reg_tok:
                return "bit", {"register": reg_tok, "bit": bit_tok}
        return "register", {"register": candidates[0]}

    # 6. Default: semantic search
    return "search", {}
