"""LaTeX / troff documentation extraction (scripts/markup_text.py) and its loader wiring."""

import build_corpus
import markup_text

TEX = r"""\documentclass{book}
\usepackage{amsmath}
\newcommand{\ct}[1]{\texttt{#1}}
\begin{document}
% a comment that must disappear
\chapter{Thermal Radiation}
\label{chap:radiation}
The radiative transport equation (RTE) is solved with the finite volume method~\cite{Raithby,Chui}.
Heat flux of 50\% is set by \ct{RADIATIVE_FRACTION} on the \ct{REAC} line; see Section~\ref{sec:x}.
\begin{equation}
\mathbf{s} \cdot \nabla I_\lambda(\mathbf{x},\mathbf{s}) = -\kappa(\mathbf{x},\lambda)\, I_\lambda
\end{equation}
The absorption coefficient $\kappa$ uses RadCal\footnote{A narrow-band model.}.
\begin{itemize}
\item Gray gas model
\item Wide band model
\end{itemize}
\begin{lstlisting}
&RADI RADIATIVE_FRACTION=0.35 / % not a comment inside a listing
\end{lstlisting}
\begin{figure}[ht]
\includegraphics[width=3in]{FIGURES/rad}
\caption{Radiation angles.}
\end{figure}
\begin{tabular}{|l|c|}
\hline
Quantity & Units \\ \hline
HRR & kW \\
\end{tabular}
\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}
R\&D costs \{braces\} stay.
\end{document}
"""


def test_tex_keeps_prose_math_tables_and_listings():
    out = markup_text.tex_to_text(TEX)
    assert out.startswith("# Thermal Radiation")
    assert "finite volume method [Raithby, Chui]." in out
    assert "Heat flux of 50% is set by RADIATIVE_FRACTION on the REAC line" in out
    # equations stay verbatim LaTeX (content, not markup)
    assert r"\begin{equation}" in out and r"-\kappa(\mathbf{x},\lambda)" in out
    assert r"coefficient $\kappa$ uses RadCal (A narrow-band model.)." in out
    assert "- Gray gas model" in out and "- Wide band model" in out
    # listings keep every character, including a literal % that is not a comment
    assert "&RADI RADIATIVE_FRACTION=0.35 / % not a comment inside a listing" in out
    assert "Radiation angles." in out
    assert "Quantity | Units" in out and "HRR | kW" in out
    assert "R&D costs {braces} stay." in out


def test_tex_drops_preamble_comments_and_non_text_markup():
    out = markup_text.tex_to_text(TEX)
    for junk in ("documentclass", "usepackage", "newcommand", "a comment that", "chap:radiation",
                 "includegraphics", "FIGURES/rad", "tikzpicture", r"\draw", "hline", r"\ct"):
        assert junk not in out, junk


def test_tex_without_document_environment_is_a_chapter_file():
    out = markup_text.tex_to_text("\\section{Plume}\nThe \\emph{plume} rises.\n")
    assert out == "## Plume\n\nThe plume rises.\n"


TROFF = r""".\" RCSid "$Id: rpict.1,v 1.34 $"
.TH RPICT 1 2/26/99 RADIANCE
.SH NAME
rpict - generate a RADIANCE picture
.SH DESCRIPTION
.I Rpict
generates a picture from the scene given in
.I octree
and sends it to the standard output.
.PP
Options are:
.nf

	rpict -vp 0 0 1 scene.oct

.fi
.TP
.BI -vh \ val
Set the view horizontal size to
.IR val .
.de XX
junk macro body
..
.SH "SEE ALSO"
oconv(1), rview(1) \(em and \fBrtrace\fR(1).
"""


def test_troff_man_page_to_paragraphs():
    out = markup_text.troff_to_text(TROFF)
    assert out.startswith("# RPICT(1)")
    assert "## NAME\nrpict - generate a RADIANCE picture" in out
    assert ("Rpict generates a picture from the scene given in octree and sends it to the "
            "standard output.") in out
    assert "\trpict -vp 0 0 1 scene.oct" in out  # no-fill example keeps its line
    assert "-vh val\n\nSet the view horizontal size to val." in out
    assert "## SEE ALSO\noconv(1), rview(1) \u2014 and rtrace(1)." in out
    assert "RCSid" not in out and "junk macro" not in out and ".TP" not in out


def test_troff_ms_numbered_heading_takes_following_line():
    out = markup_text.troff_to_text(".NH 1\nIntroduction\n.PP\nRADIANCE was developed.\n")
    assert out == "## Introduction\n\nRADIANCE was developed.\n"


def test_loader_routes_markup_formats():
    assert build_corpus.extract_for("tex", b"\\section{A}\nB \\textbf{C}.") == "## A\n\nB C."
    assert build_corpus.extract_for("troff", b".SH X\nword\n") == "## X\nword"
    record, ext = build_corpus._new_record({"id": "gh-x", "url": "https://e/x.tex",
                                            "format": "tex"})
    assert ext == "tex" and record["format"] == "tex"


def test_extract_html_keeps_sphinx_link_text_but_drops_wiki_citation_markers():
    html = (b'<html><body><div role="main"><p>See <a class="reference internal" '
            b'href="run.html"><span>Running a Project</span></a> for details'
            b'<sup class="reference"><a href="#cite">[1]</a></sup>.</p></div></body></html>')
    out = build_corpus.extract_html(html)
    assert "Running a Project" in out
    assert "[1]" not in out


# --- review regressions (2026-09-24) ------------------------------------------------------------

def test_box_wrappers_keep_their_content_argument():
    src = (r"\resizebox{\textwidth}{!}{\begin{tabular}{lr}Concrete & 2400 \\ Steel & 7850"
           r"\end{tabular}}" "\n"
           r"\scalebox{0.8}[1]{Scaled note.} \raisebox{2pt}[0pt][0pt]{Raised.} "
           r"\textcolor{red}{Warning text} \label{tab:x}{Kept after label.}")
    out = markup_text.tex_to_text(src)
    assert "Concrete | 2400" in out and "Steel | 7850" in out
    assert "Scaled note." in out and "Raised." in out
    assert "Warning text" in out and "red" not in out
    assert "Kept after label." in out and "tab:x" not in out
    assert "textwidth" not in out and "[1]" not in out and "0pt" not in out


def test_inline_verbatim_is_protected_before_comment_stripping():
    out = markup_text.tex_to_text("Use \\verb|50%| for the limit. % real comment\n"
                                  "Also \\lstinline{&REAC FUEL='X' /} here.\n")
    assert "Use 50% for the limit." in out
    assert "real comment" not in out
    assert "Also &REAC FUEL='X' / here." in out


def test_listing_body_starting_with_a_brace_is_not_eaten_as_an_argument():
    src = ("\\begin{lstlisting}\n{\"zone\": \"Office\", \"area\": 50}\n% keep me\n"
           "\\end{lstlisting}\n\\begin{lstlisting}[language=Python]\nx = 1\n\\end{lstlisting}\n"
           "\\begin{minted}{python}\ny = 2\n\\end{minted}\n")
    out = markup_text.tex_to_text(src)
    assert '{"zone": "Office", "area": 50}' in out
    assert "% keep me" in out
    assert "x = 1" in out and "language=Python" not in out
    assert "y = 2" in out and "python" not in out


def test_troff_decodes_greek_and_keeps_unresolved_escapes_visible():
    out = markup_text.troff_to_text(
        ".PP\nThe ratio \\(*g = 3 and \\(*h, \\(*q, \\(*W, \\[*a] with \\(zz and \\*(ZZ kept;"
        " a \\\\ backslash.\n")
    assert "\u03b3 = 3" in out
    assert "\u03b8, \u03c8, \u03a9, \u03b1" in out
    assert "\\(zz" in out and "\\*(ZZ" in out
    assert "a \\ backslash." in out
