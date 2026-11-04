//! Text rules of the reference implementation, reproduced where the bus
//! stores or compares their result: `str.strip()`, `str.lower()`, `repr()`
//! of a list of names in messages, `fnmatch.fnmatchcase` and the JSON
//! layout of `json.dumps`.

use std::io;

use serde::Serialize;
use serde_json::ser::{Formatter, PrettyFormatter, Serializer};

/// Python's `str.isspace()`: Unicode white space plus the information
/// separators U+001C–U+001F, which Rust does not count as white space.
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_py_space)
}

/// Python's `text.strip().lower()`, the normal form of an agent name.
pub fn fold(text: &str) -> String {
    strip(text).to_lowercase()
}

/// Python's `repr()` of a string, quoted as Python chooses.
pub fn repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c == '\u{7f}' => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr()` of a list of strings: `['a', 'b']`.
pub fn repr_list(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| repr_str(s)).collect();
    format!("[{}]", inner.join(", "))
}

enum Token {
    Star,
    Any,
    Char(char),
    Set { negate: bool, ranges: Vec<(char, char)> },
}

fn tokenize(pattern: &[char]) -> Vec<Token> {
    let mut tokens = Vec::new();
    let mut i = 0;
    while let Some(&c) = pattern.get(i) {
        i += 1;
        match c {
            '*' => {
                if !matches!(tokens.last(), Some(Token::Star)) {
                    tokens.push(Token::Star);
                }
            }
            '?' => tokens.push(Token::Any),
            '[' => {
                let mut j = i;
                if pattern.get(j) == Some(&'!') {
                    j += 1;
                }
                if pattern.get(j) == Some(&']') {
                    j += 1;
                }
                while pattern.get(j).is_some_and(|&c| c != ']') {
                    j += 1;
                }
                if j >= pattern.len() {
                    tokens.push(Token::Char('['));
                    continue;
                }
                let mut body = &pattern[i..j];
                i = j + 1;
                let negate = body.first() == Some(&'!');
                if negate {
                    body = &body[1..];
                }
                let mut ranges = Vec::new();
                let mut k = 0;
                while let Some(&lo) = body.get(k) {
                    if body.get(k + 1) == Some(&'-') && k + 2 < body.len() {
                        let hi = body[k + 2];
                        if lo <= hi {
                            ranges.push((lo, hi));
                        }
                        k += 3;
                    } else {
                        ranges.push((lo, lo));
                        k += 1;
                    }
                }
                tokens.push(Token::Set { negate, ranges });
            }
            c => tokens.push(Token::Char(c)),
        }
    }
    tokens
}

fn matches_from(tokens: &[Token], name: &[char]) -> bool {
    match tokens.split_first() {
        None => name.is_empty(),
        Some((Token::Star, rest)) => (0..=name.len()).any(|skip| matches_from(rest, &name[skip..])),
        Some((token, rest)) => match name.split_first() {
            None => false,
            Some((&c, tail)) => {
                let one = match token {
                    Token::Any => true,
                    Token::Char(expected) => c == *expected,
                    Token::Set { negate, ranges } => {
                        ranges.iter().any(|&(lo, hi)| lo <= c && c <= hi) != *negate
                    }
                    Token::Star => false,
                };
                one && matches_from(rest, tail)
            }
        },
    }
}

/// Python's `fnmatch.fnmatchcase(name, pattern)`: `*`, `?`, `[set]`,
/// `[!set]`, an unclosed `[` taken literally.
pub fn fnmatchcase(name: &str, pattern: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let name: Vec<char> = name.chars().collect();
    matches_from(&tokenize(&pattern), &name)
}

/// The compact layout of Python's `json.dumps`: `", "` between items,
/// `": "` after a key.
struct PyCompact;

impl Formatter for PyCompact {
    fn begin_array_value<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }

    fn begin_object_key<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }

    fn begin_object_value<W: ?Sized + io::Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
}

fn render<F: Formatter>(value: &impl Serialize, formatter: F) -> Result<String, serde_json::Error> {
    let mut out = Vec::new();
    value.serialize(&mut Serializer::with_formatter(&mut out, formatter))?;
    // serde_json writes valid UTF-8 only.
    Ok(String::from_utf8_lossy(&out).into_owned())
}

/// `json.dumps(value, ensure_ascii=False)`.
pub fn dumps(value: &impl Serialize) -> Result<String, serde_json::Error> {
    render(value, PyCompact)
}

/// `json.dumps(value, indent=2, ensure_ascii=False)`.
pub fn dumps_indented(value: &impl Serialize) -> Result<String, serde_json::Error> {
    render(value, PrettyFormatter::with_indent(b"  "))
}

/// `ensure_ascii=True` applied to a serialized document: every non-ASCII
/// character (necessarily inside a string) as a `\uXXXX` escape, UTF-16
/// surrogate pairs beyond the basic plane.
pub fn ascii_escape(json_text: &str) -> String {
    let mut out = String::with_capacity(json_text.len());
    for c in json_text.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn strip_should_drop_python_white_space() {
        assert_eq!(strip("\u{1f} a b\n\t"), "a b");
    }

    #[test]
    fn repr_list_should_quote_like_python() {
        let items = vec!["a".to_owned(), "it's".to_owned()];
        assert_eq!(repr_list(&items), "['a', \"it's\"]");
    }

    #[test]
    fn fnmatchcase_should_handle_star_and_question_mark() {
        assert!(fnmatchcase("claude-opus-4", "claude-*-?"));
        assert!(!fnmatchcase("gpt-4", "claude-*"));
    }

    #[test]
    fn fnmatchcase_should_handle_sets() {
        assert!(fnmatchcase("b", "[a-c]"));
        assert!(fnmatchcase("x", "[!a-c]"));
        assert!(!fnmatchcase("b", "[!a-c]"));
        assert!(fnmatchcase("]", "[]]"));
    }

    #[test]
    fn fnmatchcase_should_take_an_unclosed_bracket_literally() {
        assert!(fnmatchcase("[a", "[a"));
    }

    #[test]
    fn dumps_should_use_python_separators() {
        let value = json!({"a": [1, 2], "b": "é"});
        assert_eq!(dumps(&value).unwrap(), "{\"a\": [1, 2], \"b\": \"é\"}");
    }

    #[test]
    fn dumps_indented_should_match_python_indent_2() {
        let value = json!({"a": [], "b": {"c": 1}});
        assert_eq!(
            dumps_indented(&value).unwrap(),
            "{\n  \"a\": [],\n  \"b\": {\n    \"c\": 1\n  }\n}"
        );
    }
}
