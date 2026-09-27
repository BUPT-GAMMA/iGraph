"""Small expression admission boundary; not a general Cypher type checker.

Only literal lists can construct heap values. Indexing selects a single value;
array slicing and maps are outside this envelope. '+' must have numeric atoms,
so neither aliases nor database property names are used as type evidence.
"""
import re

from .falkor_protocol import reject
from .policy import _TOKENS

_NUMBER = r'\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
_LEX = re.compile(_NUMBER + '|' + _TOKENS.pattern, re.DOTALL)
_KEYWORDS = set('match optional where with return unwind call yield as distinct '
                'order by skip limit union all case when then else end in is '
                'null true false not and or xor ascending descending asc desc'.split())
# Tagged 4.20.7 conversion functions return a number/null or an error, never heap.
_NUMERIC = {'tofloat', 'tointeger'}


def check_expressions(query):
    tokens = []
    costs = []
    for match in _LEX.finditer(query):
        value = match.group()
        if value.isspace() or value.startswith(('//', '/*')):
            continue
        tokens.append('<literal>' if value.startswith(("'", '"')) else value.lower())
        costs.append(len(value.encode('utf-8')))
    pairs = {}
    stack = []
    for i, token in enumerate(tokens):
        if token in {'{', '}'}:
            reject()
        if token in {'(', '['}:
            stack.append(i)
            if len(stack) > 32:
                reject()
        elif token in {')', ']'}:
            if not stack or tokens[stack[-1]] != {')': '(', ']': '['}[token]:
                reject()
            start = stack.pop()
            pairs[start] = i
            pairs[i] = start
    if stack:
        reject()

    # Global source/storage bound for all constructors, including nested copies.
    # At most 128 scalar/list entries, 4 levels and 4096 UTF-8 source bytes.
    entries = literal_bytes = 0
    literal_brackets = set()

    def literal_list(start, depth):
        nonlocal entries, literal_bytes
        if depth > 4:
            reject()
        end = pairs[start]
        literal_brackets.add(start)
        pos = start + 1
        while pos < end:
            entries += 1
            if entries > 128:
                reject()
            if tokens[pos] == '[':
                pos = literal_list(pos, depth + 1) + 1
            else:
                if tokens[pos] in {'+', '-'}:
                    pos += 1
                    if pos >= end or not re.fullmatch(_NUMBER, tokens[pos]):
                        reject()
                if not (tokens[pos] in {'<literal>', 'true', 'false', 'null'}
                        or re.fullmatch(_NUMBER, tokens[pos])):
                    reject()
                pos += 1
            if pos < end:
                if tokens[pos] != ',':
                    reject()
                pos += 1
                if pos == end:
                    reject()
        if depth == 1:
            literal_bytes += sum(costs[start:end + 1])
            if literal_bytes > 4096:
                reject()
        return end

    def identifier(token):
        return token.startswith('`') or bool(re.fullmatch(r'[^\W\d]\w*', token))

    in_match = False
    for i, token in enumerate(tokens):
        previous = tokens[i - 1] if i else ''
        if token in _KEYWORDS and previous not in {'.', ':', '$'}:
            if token == 'match':
                in_match = True
            elif token in {'where', 'with', 'return', 'unwind', 'call'}:
                in_match = False
        if token != '[' or i in literal_brackets:
            continue
        end = pairs[i]
        # Relationship brackets are admitted only in MATCH patterns. A similar
        # sequence in an arithmetic expression must not disguise a constructor.
        if in_match and previous == '-' and tokens[i - 2:i] in [[')', '-'], ['<', '-']]:
            if end + 1 >= len(tokens) or tokens[end + 1] != '-':
                reject()
            if any(not (identifier(t) or t == ':') for t in tokens[i + 1:end]):
                reject()
            continue
        index = previous in {')', ']'} or (identifier(previous) and previous not in _KEYWORDS)
        if index:
            # Slices copy arbitrary database arrays; single-index lookup remains.
            if any(tokens[j:j + 2] == ['.', '.'] for j in range(i + 1, end)):
                reject()
        else:
            literal_list(i, 1)

    def numeric_atom(start):
        """Return the end of a provably numeric atom, or None (no alias inference)."""
        while start < len(tokens) and tokens[start] in {'+', '-'}:
            start += 1
        if start >= len(tokens):
            return None
        token = tokens[start]
        if re.fullmatch(_NUMBER, token):
            return start + 1
        if token in _NUMERIC and tokens[start + 1:start + 2] == ['(']:
            return pairs[start + 1] + 1
        if token == '(':
            end = pairs[start]
            pos = numeric_atom(start + 1)
            while pos is not None and pos < end and tokens[pos] in {'+', '-', '*', '/', '%', '^'}:
                pos = numeric_atom(pos + 1)
            return end + 1 if pos == end else None
        return None

    for i, token in enumerate(tokens):
        if token != '+':
            continue
        left = i - 1
        if left >= 0 and tokens[left] == ')':
            left = pairs[left]
            if left and tokens[left - 1] in _NUMERIC:
                left -= 1
        # A literal unary plus is allowed; otherwise prove both adjacent atoms.
        unary = i == 0 or tokens[i - 1] in {'(', '[', ',', 'return', 'with', '+', '-', '*', '/', '%', '^'}
        if not unary and (left < 0 or numeric_atom(left) != i
                          or (left and tokens[left - 1] in {'.', '$'})):
            reject()
        if numeric_atom(i + 1) is None:
            reject()
