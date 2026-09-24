#!/usr/bin/env python3
"""markup_text.py — conservative text extraction for documentation markup the loader stores.

Some of the most valuable open building-simulation manuals are not Markdown or HTML: the NIST
FDS / CFAST user guides and technical references are LaTeX, and the Radiance reference manual and
~150 man pages are troff. `build_corpus.extract_for` routes the registry formats `tex` and `troff`
here. Both converters are deliberately CONSERVATIVE:

* they never paraphrase or reorder prose — they only remove markup tokens;
* mathematics is kept verbatim as LaTeX (`$...$`, `\\[...\\]`, equation environments) because
  equations are content (AGENTS.md: preserve equations and numeric tables);
* tables keep every cell (`a | b | c` rows), code listings keep every line;
* unknown commands are unwrapped (their braced argument text is kept) rather than dropped, so a
  project-specific macro such as FDS's `\\ct{MATL}` degrades to its text, never to nothing.

Only markup with no reading value is removed: comments, the preamble, figure-drawing
environments, `\\label` / `\\index` / `\\includegraphics` / spacing commands, and troff requests.
"""
from __future__ import annotations

import re

# ----------------------------------------------------------------------------------------------
# LaTeX
# ----------------------------------------------------------------------------------------------

_MATH_ENVS = ("equation", "equation*", "align", "align*", "alignat", "alignat*", "eqnarray",
              "eqnarray*", "gather", "gather*", "multline", "multline*", "displaymath", "math",
              "flalign", "flalign*")
# Environments whose body is drawing code, not prose.
_DROP_ENVS = ("tikzpicture", "pgfpicture", "picture", "comment", "filecontents",
              "filecontents*")
# \begin{env} takes these many mandatory {args} that are layout, not text.
_ENV_ARGS = {"tabular": 1, "tabular*": 2, "tabularx": 2, "longtable": 1, "minipage": 1,
             "array": 1, "multicols": 1, "wrapfigure": 2, "supertabular": 1, "xtabular": 1,
             "minted": 1}
_HEADINGS = {"part": "#", "chapter": "#", "section": "##", "subsection": "###",
             "subsubsection": "####", "paragraph": "", "subparagraph": ""}
# Commands removed together with ALL their arguments (no reading value).
# Command -> number of MANDATORY {..} arguments it takes (optional [..] ones are skipped too).
# Exact arities matter: consuming "all following groups" would swallow the text after e.g.
# `\label{x}{\bf Note}`.
_DROP_WITH_ARGS = {
    "label": 1, "index": 1, "includegraphics": 1, "vspace": 1, "vspace*": 1, "hspace": 1,
    "hspace*": 1, "input": 1, "include": 1, "bibliography": 1, "bibliographystyle": 1,
    "pagestyle": 1, "thispagestyle": 1, "setlength": 2, "addtolength": 2, "addtocontents": 2,
    "addcontentsline": 3, "needspace": 1, "rule": 2, "newcommand": 2, "renewcommand": 2,
    "providecommand": 2, "newenvironment": 3, "renewenvironment": 3, "color": 1,
    "definecolor": 3, "hypersetup": 1, "usepackage": 1, "documentclass": 1, "markboth": 2,
    "markright": 1, "fontsize": 2, "pagenumbering": 1, "setcounter": 2, "addtocounter": 2,
    "graphicspath": 1, "lstset": 1, "lstdefinestyle": 2, "newlength": 1, "settowidth": 2,
    "linethickness": 1, "includepdf": 1, "nocite": 1, "newtheorem": 2, "captionsetup": 1,
    "extracolsep": 1, "enlargethispage": 1, "titleformat": 5, "newpage": 0, "clearpage": 0,
    "cleardoublepage": 0, "multiput": 4, "put": 1,
}
# Box/colour wrappers: skip this many LAYOUT arguments, then keep the CONTENT argument
# (`\resizebox{\textwidth}{!}{<table>}` must keep the table).
_CONTENT_AFTER = {
    "resizebox": 2, "resizebox*": 2, "scalebox": 1, "raisebox": 1, "rotatebox": 1,
    "parbox": 1, "textcolor": 1, "colorbox": 1, "fcolorbox": 2, "makebox": 0, "framebox": 0,
    "mbox": 0, "fbox": 0, "adjustbox": 1,
}
# Commands whose single braced argument is emitted in brackets (citations).
_CITE = {"cite", "citep", "citet", "citealp", "citeauthor", "citeyear", "parencite",
         "textcite", "footcite"}
# Commands that take one argument that is a cross-reference key.
_REF = {"ref", "eqref", "pageref", "autoref", "cref", "Cref", "nameref", "vref"}
_SPECIAL = {"%": "%", "&": "\x01", "_": "_", "#": "#", "$": "$", "{": "\x02", "}": "\x03",
            " ": " ", ",": " ", ";": " ", ":": " ", "!": "", "/": "", "-": "", "@": "",
            "\\": "\n", "textbackslash": "\\", "ldots": "...", "dots": "...", "LaTeX": "LaTeX",
            "TeX": "TeX", "S": "§", "P": "¶", "copyright": "©", "textregistered": "®",
            "texttrademark": "™", "textdegree": "°", "degree": "°", "pounds": "£", "euro": "€",
            "item": "\n- ", "par": "\n\n", "newline": "\n", "linebreak": "\n",
            "textendash": "–", "textemdash": "—", "textasciitilde": "~", "quad": " ",
            "qquad": " ", "noindent": "", "centering": "", "degreeCelsius": "\u00b0C",
            "celsius": "\u00b0C", "textperthousand": "\u2030", "textmu": "\u00b5"}
_PLACEHOLDER = "\x00M{}\x00"
_PH_RE = re.compile(r"\x00M(\d+)\x00")


# Arguments a verbatim environment takes after \begin{env}: "o" optional [..], "m" mandatory {..}.
# Its BODY starts only after these, so a listing whose first line begins with "{" keeps it.
_VERBATIM_ARGS = {"verbatim": "", "verbatim*": "", "alltt": "", "lstlisting": "o",
                  "Verbatim": "o", "minted": "om"}


def _protect_literals(src: str, protect) -> str:
    """One left-to-right pass that FIRST shelters literal text, then strips comments.

    Literal text — verbatim-like environment bodies and inline \\verb / \\lstinline — is swapped
    for placeholders before anything else looks at the source, so a `%` inside it is never read
    as a comment (`\\verb|50%|` stays `50%`). Outside literals, an unescaped `%` starts a comment
    that runs to the end of the line; a line that was only a comment disappears entirely.
    """
    out: list[str] = []
    i, n = 0, len(src)
    line_has_text = False
    while i < n:
        c = src[i]
        if c == "\\":
            m = re.match(r"\\(verb\*?|lstinline)", src[i:])
            if m:
                j = i + m.end()
                if j < n and src[j] == "{" and m.group(1) == "lstinline":
                    g = _group(src, j)
                    if g:
                        out.append(protect(g[0]))
                        i = g[1]
                        line_has_text = True
                        continue
                if j < n and not src[j].isalpha() and not src[j].isspace():
                    end = src.find(src[j], j + 1)
                    if end != -1 and "\n" not in src[j + 1:end]:
                        out.append(protect(src[j + 1:end]))
                        i = end + 1
                        line_has_text = True
                        continue
            m = re.match(r"\\begin\{(" + "|".join(map(re.escape, _VERBATIM_ARGS)) + r")\}",
                         src[i:])
            if m:
                env = m.group(1)
                j = i + m.end()
                for kind in _VERBATIM_ARGS[env]:
                    g = _group(src, j, "[", "]") if kind == "o" else _group(src, j)
                    if g:
                        j = g[1]
                if j < n and src[j] == "\n":
                    j += 1
                end = src.find("\\end{" + env + "}", j)
                if end != -1:
                    out.append(protect("\n" + src[j:end].rstrip() + "\n"))
                    i = end + len("\\end{" + env + "}")
                    line_has_text = True
                    continue
            out.append(src[i:i + 2])
            i += 2
            line_has_text = True
            continue
        if c == "%":
            eol = src.find("\n", i)
            eol = n if eol == -1 else eol
            # a comment-only line vanishes with its newline; a trailing comment keeps the break
            i = eol + 1 if not line_has_text and eol < n else eol
            continue
        if c == "\n":
            line_has_text = False
        elif not c.isspace():
            line_has_text = True
        out.append(c)
        i += 1
    return "".join(out)


def _group(s: str, i: int, open_: str = "{", close: str = "}") -> tuple[str, int] | None:
    """If s[i] (after spaces) opens a balanced group, return (inner text, index after it)."""
    j = i
    while j < len(s) and s[j] in " \t":
        j += 1
    if j >= len(s) or s[j] != open_:
        return None
    depth, k = 0, j
    while k < len(s):
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return s[j + 1:k], k + 1
        k += 1
    return None


def _skip_args(s: str, i: int, mandatory: int | None = None) -> int:
    """Skip optional [..] groups and up to `mandatory` {..} groups (None = all that follow)."""
    taken = 0
    while True:
        if mandatory is not None and taken >= mandatory:
            return i
        g = _group(s, i, "[", "]")
        if g:
            i = g[1]
            continue
        g = _group(s, i)
        if not g:
            return i
        i = g[1]
        taken += 1


def _skip_optional(s: str, i: int) -> int:
    """Skip optional [..] groups immediately following position i."""
    while (g := _group(s, i, "[", "]")) is not None:
        i = g[1]
    return i


def tex_to_text(src: str) -> str:
    """LaTeX source -> readable text (math kept as LaTeX, markup removed)."""
    protected: list[str] = []

    def protect(text: str) -> str:
        protected.append(text)
        return _PLACEHOLDER.format(len(protected) - 1)

    src = src.replace("\r\n", "\n").replace("\x00", "")
    # literals first (listings, \verb): nothing after this may reinterpret their characters
    src = _protect_literals(src, protect)
    src = re.sub(r"\\iffalse\b.*?\\fi\b", "", src, flags=re.S)
    m = re.search(r"\\begin\{document\}", src)
    if m:
        src = src[m.end():]
    src = re.split(r"\\end\{document\}", src)[0]
    for env in _DROP_ENVS:
        src = re.sub(r"\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}",
                     "", src, flags=re.S)
    # mathematics, kept verbatim with its delimiters
    maths = "|".join(map(re.escape, _MATH_ENVS))
    src = re.sub(r"\\begin\{(" + maths + r")\}.*?\\end\{\1\}", lambda m: protect(m.group(0)),
                 src, flags=re.S)
    src = re.sub(r"\\be\b.*?\\ee\b", lambda m: protect(m.group(0)), src, flags=re.S)
    src = re.sub(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)", lambda m: protect(m.group(0)), src,
                 flags=re.S)
    src = re.sub(r"(?<!\\)\$(?:\\.|[^$\\])+?\$", lambda m: protect(m.group(0)), src,
                 flags=re.S)

    text = _tex_commands(src)
    # tables: cells separated by " | ", rows by newlines
    text = re.sub(r"[ \t]*(?<!\\)&[ \t]*", " | ", text)
    text = text.replace("~", " ").replace("``", "\u201c").replace("''", "\u201d")
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    text = re.sub(r"(?<!\\)[{}]", "", text)
    text = text.replace("\x01", "&").replace("\x02", "{").replace("\x03", "}")
    text = _PH_RE.sub(lambda m: protected[int(m.group(1))], text)
    return _tidy(text)


def _tex_commands(s: str) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        m = re.match(r"\\([a-zA-Z@]+\*?|.)", s[i:])
        if not m:
            i += 1
            continue
        name, j = m.group(1), i + m.end()
        if name in ("begin", "end"):
            g = _group(s, j)
            env = g[0] if g else ""
            j = g[1] if g else j
            if name == "begin":
                j = _skip_args(s, _skip_optional(s, j), _ENV_ARGS.get(env, 0))
            if env in ("itemize", "enumerate", "description", "abstract", "quote",
                       "quotation", "center", "figure", "figure*", "table", "table*"):
                out.append("\n")
            i = j
            continue
        if name in _HEADINGS:
            j = _skip_optional(s, j)
            g = _group(s, j)
            if g:
                mark = _HEADINGS[name]
                out.append(f"\n\n{mark + ' ' if mark else ''}{_tex_commands(g[0]).strip()}\n\n")
                i = g[1]
                continue
        if name in ("caption", "footnote", "footnotetext", "title", "author", "date",
                    "chapter*", "section*", "subsection*", "subsubsection*"):
            j = _skip_optional(s, j)
            g = _group(s, j)
            if g:
                inner = _tex_commands(g[0]).strip()
                if name.endswith("*"):
                    mark = _HEADINGS.get(name[:-1], "")
                    out.append(f"\n\n{mark + ' ' if mark else ''}{inner}\n\n")
                elif name.startswith("footnote"):
                    out.append(f" ({inner})")
                else:
                    out.append(f"\n{inner}\n")
                i = g[1]
                continue
        if name in _DROP_WITH_ARGS:
            i = _skip_optional(s, _skip_args(s, j, _DROP_WITH_ARGS[name]))
            continue
        if name in _CONTENT_AFTER:
            # drop the layout arguments; the content group stays in the stream and is unwrapped
            i = _skip_optional(s, _skip_args(s, _skip_optional(s, j), _CONTENT_AFTER[name]))
            continue
        if name in _CITE or name in _REF:
            j = _skip_optional(s, j)
            g = _group(s, j)
            if g:
                keys = ", ".join(k.strip() for k in g[0].split(","))
                out.append(f"[{keys}]" if name in _CITE else keys)
                i = g[1]
                continue
        if name in ("href", "texorpdfstring"):  # keep the human-readable argument only
            g1 = _group(s, j)
            g2 = _group(s, g1[1]) if g1 else None
            if g1 and g2:
                out.append(_tex_commands(g2[0] if name == "href" else g1[0]))
                i = g2[1]
                continue
        if name == "multicolumn":
            g1 = _group(s, j)
            g2 = _group(s, g1[1]) if g1 else None
            g3 = _group(s, g2[1]) if g2 else None
            if g3:
                out.append(_tex_commands(g3[0]))
                i = g3[1]
                continue
        if name in ("hline", "cline", "toprule", "midrule", "bottomrule", "endhead",
                    "endfirsthead", "endfoot", "endlastfoot"):
            i = _skip_args(s, j, 1 if name == "cline" else 0)
            continue
        if name in _SPECIAL:
            out.append(_SPECIAL[name])
            if name in ("item", "\\"):
                j = _skip_optional(s, j)
                while name == "item" and j < n and s[j] in " \t":
                    j += 1
            i = j
            continue
        if len(name) == 1 and not name.isalpha():
            # accents like \' \" \^ \` \~ \= \. : keep the letter they decorate
            i = j
            continue
        # unknown command: drop the token, keep any braced argument text (unwrapped)
        j = _skip_optional(s, j)
        i = j
    return "".join(out)


def _tidy(text: str) -> str:
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip() in ("", "|", "| |"):
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip() + "\n"


# ----------------------------------------------------------------------------------------------
# troff (man(7) and ms(7) macro packages, as used by the Radiance manual pages)
# ----------------------------------------------------------------------------------------------

_TROFF_CHARS = {
    "em": "\u2014", "en": "\u2013", "bu": "\u2022", "co": "\u00a9", "rg": "\u00ae",
    "de": "\u00b0", "mu": "\u00d7", "di": "\u00f7", "+-": "\u00b1", "<=": "\u2264",
    ">=": "\u2265", "!=": "\u2260", "==": "\u2261", "~=": "\u2245", "~~": "\u2248",
    "**": "*", "aa": "\u00b4", "ga": "`", "ul": "_", "rs": "\\", "lq": "\u201c",
    "rq": "\u201d", "oq": "\u2018", "cq": "\u2019", "hy": "-", "mi": "\u2212", "sq": "\u25a1",
    "->": "\u2192", "<-": "\u2190", "ua": "\u2191", "da": "\u2193", "<>": "\u2194",
    "sc": "\u00a7", "ct": "\u00a2", "pl": "+", "eq": "=", "ap": "~", "pt": "\u221d",
    "if": "\u221e", "sr": "\u221a", "fm": "\u2032", "sd": "\u2033", "tm": "\u2122",
    "dg": "\u2020", "dd": "\u2021", "ps": "\u00b6", "bv": "|", "br": "\u2502", "pd": "\u2202",
    "gr": "\u2207", "is": "\u222b", "sb": "\u2282", "sp": "\u2283", "ib": "\u2286",
    "ip": "\u2287", "cu": "\u222a", "ca": "\u2229", "mo": "\u2208", "no": "\u00ac",
    "es": "\u2205", "12": "\u00bd", "14": "\u00bc", "34": "\u00be", "ci": "\u25cb",
    "Fo": "\u00ab", "Fc": "\u00bb", "Eu": "\u20ac", "Po": "\u00a3", "Ye": "\u00a5",
    "OK": "\u2713", "ss": "\u00df", "AE": "\u00c6", "ae": "\u00e6", "a\"": "\u00e4",
    "o\"": "\u00f6", "u\"": "\u00fc", "e'": "\u00e9", "e`": "\u00e8",
    "pi": "\u03c0",
}
# troff's Greek glyphs are \(*x, e.g. \(*g = gamma, \(*h = theta, \(*q = psi.
for _k, _v in zip("abgdezyhiklmncoprstufxqw", "\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b7"
                  "\u03b8\u03b9\u03ba\u03bb\u03bc\u03bd\u03be\u03bf\u03c0\u03c1\u03c3\u03c4"
                  "\u03c5\u03c6\u03c7\u03c8\u03c9"):
    _TROFF_CHARS["*" + _k] = _v
    _TROFF_CHARS["*" + _k.upper()] = _v.upper()
_TROFF_CHARS["ts"] = "\u03c2"  # terminal sigma
# Predefined strings of the man / ms packages (\*(lq, \*R ...).
_TROFF_STRINGS = {"lq": "\u201c", "rq": "\u201d", "R": "\u00ae", "Tm": "\u2122",
                  "S": "", "Q": "\u201c", "U": "\u201d", "-": "\u2014", "*": ""}
# Requests/macros whose arguments are displayed text in the man/ms packages.
_FONT_MACROS = {"B", "I", "SM", "SB", "R", "CW"}
_ALT_MACROS = {"BI", "IB", "BR", "RB", "IR", "RI"}
_BREAK_MACROS = {"PP", "LP", "P", "TP", "HP", "QP", "IP", "sp", "br", "KS", "KE", "KF", "DS",
                 "DE", "RS", "RE", "bp", "AB", "AE", "XP", "ID", "LD", "CD", "BD", "QS", "QE",
                 "Pp", "Lp", "Bl", "El", "Bd", "Ed", "It"}


def _troff_inline(s: str) -> str:
    r"""Resolve inline troff escapes. Known glyphs/strings decode to Unicode; an UNRESOLVED
    glyph or string escape is kept verbatim (e.g. `\(zz`) so no symbol silently vanishes."""
    kept: list[str] = []

    def keep(text: str) -> str:
        kept.append(text)
        return f"\x06{len(kept) - 1}\x06"

    def glyph(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return _TROFF_CHARS[name] if name in _TROFF_CHARS else keep(m.group(0))

    def string(m: re.Match) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        return _TROFF_STRINGS[name] if name in _TROFF_STRINGS else keep(m.group(0))

    s = s.replace("\\\\", "\x07")                         # literal backslash pair
    s = re.sub(r'\\".*$', "", s)                         # inline comment
    s = re.sub(r"\\f(?:\[[^\]]*\]|\([A-Za-z0-9]{2}|.)", "", s)  # font switches
    s = re.sub(r"\\s[+-]?(?:\(\d\d|\[\d+\]|\d)", "", s)  # size changes
    s = re.sub(r"\\\*(?:\[([^\]]*)\]|\((..)|(.))", string, s)  # string interpolation
    s = re.sub(r"\\\[([^\]]+)\]|\\\((..)", glyph, s)     # special characters
    s = re.sub(r"\\[hvwlLoxDbZk]'[^']*'", "", s)         # motions / drawing
    s = re.sub(r"\\n(?:\(..|\[[^\]]*\]|.)", "", s)       # number registers
    s = s.replace("\\-", "-").replace("\\e", "\\").replace("\\ ", " ").replace("\\~", " ")
    s = s.replace("\\0", " ").replace("\\t", "\t")
    s = re.sub(r"\\[&|^c%:!)/,]", "", s)
    s = s.replace("\x07", "\\")
    return re.sub("\x06(\\d+)\x06", lambda m: kept[int(m.group(1))], s)


def _troff_args(rest: str) -> list[str]:
    """Split macro arguments: blanks separate, double quotes group, a backslash escape (e.g. the
    literal blank `\\ `) stays attached to its argument."""
    args, cur, quoted, i, rest = [], "", False, 0, rest.strip()
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            cur += rest[i:i + 2]
            i += 2
            continue
        i += 1
        if ch == '"':
            quoted = not quoted
            continue
        if ch in " \t" and not quoted:
            if cur:
                args.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        args.append(cur)
    return args


_NOFILL_START = {"nf", "EQ", "TS", "DS", "CD", "LD", "ID", "BD"}
_NOFILL_END = {"fi", "EN", "TE", "DE"}


def troff_to_text(src: str) -> str:
    """troff source (man / ms macros) -> readable text; request lines become structure.

    Filled text is re-joined into paragraphs the way troff typesets it; no-fill blocks (.nf/.fi,
    eqn .EQ/.EN, tbl .TS/.TE, ms displays) keep their line breaks so command examples, equations
    and tables survive line for line.
    """
    # Each emitted item is (kind, text): "fill" lines join with their fill neighbours, "line"
    # items stand alone, "break" separates paragraphs, "head" is a heading waiting for text
    # (the ms `.NH` macro puts the heading text on the following line).
    items: list[tuple[str, str]] = []
    skip_until: str | None = None
    nofill = 0
    tag_next = False  # man .TP: the next text line is the item tag, typeset on its own line

    def text(t: str) -> None:
        nonlocal tag_next
        if tag_next:
            items.extend((("line", t), ("break", "")))
            tag_next = False
            return
        items.append(("line" if nofill else "fill", t))

    for line in src.replace("\r\n", "\n").split("\n"):
        if skip_until:
            if line.startswith(skip_until):
                skip_until = None
            continue
        if line.startswith((".\\\"", "'\\\"", "\\\"")) or line.strip() in (".", "'"):
            continue
        m = re.match(r"^[.'][ \t]*([A-Za-z0-9]{1,3})(?![A-Za-z0-9])(.*)$", line)
        if not m:
            text(_troff_inline(line))
            continue
        req, rest = m.group(1), m.group(2)
        if req in ("de", "ig"):
            skip_until = ".."
        elif req == "TH":
            a = [_troff_inline(x) for x in _troff_args(rest)]
            if a:
                items.append(("line", f"# {a[0]}" + (f"({a[1]})" if len(a) > 1 else "")))
                items.append(("break", ""))
        elif req in ("SH", "NH", "SS", "TL", "Sh", "Ss"):
            level = {"SH": "##", "Sh": "##", "NH": "##", "TL": "#"}.get(req, "###")
            words = "" if req == "NH" else _troff_inline(" ".join(_troff_args(rest))).strip()
            items.append(("break", ""))
            items.append(("line", f"{level} {words}") if words else ("head", level))
        elif req in _FONT_MACROS:
            text(_troff_inline(" ".join(_troff_args(rest))))
        elif req in _ALT_MACROS:
            text(_troff_inline("".join(_troff_args(rest))))
        elif req == "IP":
            items.append(("break", ""))
            a = _troff_args(rest)
            if a:
                tag = _troff_inline(a[0]).strip()
                items.append(("line", tag or "\u2022"))
        elif req in _NOFILL_START:
            nofill += 1
            items.append(("break", ""))
        elif req in _NOFILL_END:
            nofill = max(0, nofill - 1)
            items.append(("break", ""))
        elif req in _BREAK_MACROS or req in ("AU", "AI", "DA", "ND"):
            items.append(("break", ""))
            tag_next = req == "TP"
        # every other request (.ft .in .ti .ce .ne .so .ta .ps .vs .ad .na .hy .ll .tl ...) is
        # layout: dropped together with its arguments

    out: list[str] = []
    prev = "break"
    for kind, t in items:
        if kind == "fill" and prev == "head":
            out[-1] = f"{out[-1]} {t.strip()}"
            out.append("")
            prev = "break"
            continue
        if kind == "fill" and prev == "fill" and out:
            out[-1] = f"{out[-1]} {t}" if out[-1] else t
        elif kind == "break":
            out.append("")
        else:
            out.append(t)
        prev = kind
    return _tidy("\n".join(re.sub(r"[ \t]+", " ", ln) if not ln.startswith(("\t", "  ")) else ln
                           for ln in out))
