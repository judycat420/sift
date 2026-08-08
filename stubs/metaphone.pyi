"""Types for `metaphone` 0.6, which ships no `py.typed`.

Hand-written rather than ignored: `mypy --strict` runs with
`disallow_any_unimported`, so an untyped third-party call would poison every
signature it touches. The package exposes one function Sift uses, and its
contract is small enough to state exactly — see STANDARDS.md rule 1, which
prefers making the type real over silencing the checker.

`doublemetaphone` returns a two-element tuple: the primary code, and an
alternate code that is the empty string when the word has only one plausible
pronunciation.
"""

def doublemetaphone(text: str) -> tuple[str, str]: ...
