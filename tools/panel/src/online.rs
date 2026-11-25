//! Online assistants (prefs::Online): the chat so far sent to a provider's model with the owner's key, the answer
//! streamed back as the board's own Assistant's is (agent::Event). They have none of this board's tools: what they
//! read could only go out with the next question, and what they are asked goes to their provider anyway. Google's
//! Gemini through its own API, asked to search Google for its answer (grounding) and, when it refuses that before
//! any answer came, without; the others through the OpenAI-style chat completions they offer (Brave's Answers searches
//! the web by itself, and takes the question alone: one message). The key goes in a header only to the provider's own address: redirects are not followed (they
//! would carry it elsewhere), and no error shows it.

use crate::agent::Event;
use crate::prefs::{self, Online, Provider};
use serde_json::{json, Value};
use std::cell::Cell;
use std::io::{BufRead, BufReader, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const GEMINI: &str = "https://generativelanguage.googleapis.com/v1beta";
const CONNECT: Duration = Duration::from_secs(20);
/// The longest wait for the next piece of an answer (not for the whole of it: a long answer streams for minutes).
const IDLE: Duration = Duration::from_secs(90);
/// An answer's size at most; past it, it is cut and said so.
const MOST: u64 = 4 << 20;
/// A provider's error message at most, as shown (one sent 3 MB of it: review).
const SAID: usize = 300;

/// What an online assistant is told first: who it is and how its answer is used. (Not the owner's instructions for
/// this board's model: they are the owner's, not the provider's.)
pub fn system_prompt(o: &Online) -> String {
    format!("You are {}, asked from a small Linux board's panel, or by voice. Answer briefly and plainly: the answer may \
             be read aloud. No tables.", o.name)
}

/// One question: the chat so far (OpenAI-style messages, the system one first), its answer through `out` (Chunk as
/// it comes, then Done or Fail).
pub fn ask(o: &Online, messages: &[Value], stop: &AtomicBool, out: &dyn Fn(Event)) {
    ask_at(GEMINI, o, messages, stop, out)
}

fn ask_at(gemini_base: &str, o: &Online, messages: &[Value], stop: &AtomicBool, out: &dyn Fn(Event)) {
    if o.key.is_empty() {
        return out(Event::Fail(format!("{} has no key yet: add it in Settings (from {})", o.name, o.provider.keys_at())));
    }
    if let Some(why) = prefs::key_problem(&o.key) {
        return out(Event::Fail(format!("{}: {why}", o.name)));
    }
    let t0 = Instant::now();
    // whether any of the answer went out: a refusal after that is no reason to ask again (the screen would show a
    // half answer above the other: review)
    let sent = Cell::new(false);
    let watched = |ev: Event| {
        if matches!(ev, Event::Chunk(_)) {
            sent.set(true);
        }
        out(ev)
    };
    // what a provider says goes on screen, into the journal and a notification: never with the key in it (one that
    // echoes the request would: review 2026-11-19)
    let hide = |s: String| if o.key.len() >= 4 { s.replace(&o.key, "…") } else { s };
    let r = match o.provider {
        // (a key refused is refused without the search too: not asked again, board test 2026-11-19; nor after Stop)
        Provider::Google => match gemini(gemini_base, o, messages, true, stop, &watched) {
            Err(e) if e.code == 400 && !e.key_refused() && !sent.get() && !stop.load(Ordering::SeqCst) => {
                out(Event::Note(hide(format!("{}: it would not search Google for this ({}); asked without searching", o.name, e.said))));
                gemini(gemini_base, o, messages, false, stop, &watched)
            }
            r => r,
        },
        _ => openai(&o.url(), o, messages, stop, &watched),
    };
    match r {
        Ok((text, tokens)) => {
            let secs = t0.elapsed().as_secs_f32().max(0.1);
            out(Event::Done { text, tok_s: tokens as f32 / secs, tokens })
        }
        Err(e) => out(Event::Fail(hide(format!("{}: {}", o.name, e.said())))),
    }
}

/// The settings page's Test: a short question, its answer or why there is none.
pub fn test(o: &Online) -> Result<String, String> {
    let messages = vec![json!({"role": "system", "content": system_prompt(o)}),
                        json!({"role": "user", "content": "Say hello in five words."})];
    let answer = std::cell::RefCell::new(Err("no answer".to_string()));
    ask(o, &messages, &AtomicBool::new(false), &|ev| match ev {
        Event::Done { text, .. } => *answer.borrow_mut() = Ok(text),
        Event::Fail(e) => *answer.borrow_mut() = Err(e),
        _ => {}
    });
    answer.into_inner()
}

/// Why there is no answer: the provider's refusal (its HTTP status and what it said, shortened), or another reason.
#[derive(Debug)]
struct Refused {
    code: u16,
    said: String,
}

impl Refused {
    fn other(said: impl Into<String>) -> Refused {
        Refused { code: 0, said: said.into() }
    }

    /// The key, by the status or by the provider's own words (Google says it with 400, Brave with 422).
    fn key_refused(&self) -> bool {
        let s = self.said.to_lowercase();
        matches!(self.code, 401 | 403)
            || ((400..500).contains(&self.code) && s.contains("key") && ["invalid", "not valid", "incorrect", "expired"].iter().any(|w| s.contains(w)))
    }

    /// In plain words, the usual reasons named.
    fn said(&self) -> String {
        let why = match self.code {
            0 => return self.said.clone(),
            300..=399 => return format!("it sent the question to another address (HTTP {}): not followed, the key would go with it", self.code),
            _ if self.key_refused() => "the key was refused",
            404 => "no such model or address",
            429 => "too many questions, or the key's quota is used up",
            500..=599 => "the provider is having trouble",
            _ => "the question was refused",
        };
        match self.code {
            200 => format!("{why} ({})", self.said), // (an error in an answer: its status says nothing)
            code => format!("{why} (HTTP {code}: {})", self.said),
        }
    }
}

/// What a provider says when it refuses: its own message (shortened), from its body (OpenAI's and Google's
/// error.message, Brave's error.detail and what in the request it could not take, error itself, or the body).
fn refusal(code: u16, body: &str) -> Refused {
    let v: Value = serde_json::from_str(body).unwrap_or(Value::Null);
    let e = &v["error"];
    let mut said = e["message"].as_str().or(e["detail"].as_str()).or(e.as_str()).or(v["message"].as_str())
        .or(v["detail"].as_str()).map(str::to_string).unwrap_or_else(|| body.to_string());
    if let Some(what) = e["meta"]["errors"][0]["msg"].as_str() {
        said = format!("{said}: {what}");
    }
    Refused { code, said: said.trim().chars().take(SAID).collect() }
}

/// A request that did not get through, in words that hold nothing of the request itself (ureq's own message for a
/// bad header held the header: the key, review 2026-11-19).
fn unreached(e: ureq::Error) -> Refused {
    match e {
        ureq::Error::Status(code, r) => refusal(code, &r.into_string().unwrap_or_default()),
        ureq::Error::Transport(t) => Refused::other(match t.kind() {
            ureq::ErrorKind::Dns => "cannot reach it: its address was not found",
            ureq::ErrorKind::ConnectionFailed => "cannot reach it: the connection failed",
            ureq::ErrorKind::Io => "cannot reach it: the connection broke, or it took too long",
            ureq::ErrorKind::InvalidUrl | ureq::ErrorKind::UnknownScheme => "cannot reach it: its address is not one",
            ureq::ErrorKind::BadHeader => "cannot reach it: the key has a character a request cannot carry",
            ureq::ErrorKind::TooManyRedirects => "it sent the question to another address: not followed, the key would go with it",
            _ => "cannot reach it",
        }),
    }
}

/// A response that sends the question elsewhere is not followed (the key would go with it): said so.
fn here(resp: ureq::Response) -> Result<ureq::Response, Refused> {
    if (300..400).contains(&resp.status()) {
        return Err(Refused { code: resp.status(), said: String::new() });
    }
    Ok(resp)
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new().timeout_connect(CONNECT).timeout_read(IDLE).redirects(0).user_agent("a7a-panel").build()
}

/// The answer from a response that is server-sent events (each `data:` line one piece, `pick` finding its text) or,
/// from a provider that does not stream, one JSON body (`whole` finding its text). Returns (text, tokens). An
/// answer that stops coming, or grows past MOST, ends there: what came is kept, and said to be cut.
fn read_answer(resp: ureq::Response, stop: &AtomicBool, out: &dyn Fn(Event), pick: impl Fn(&Value) -> String,
               whole: impl Fn(&Value) -> String, tokens: impl Fn(&Value) -> Option<u32>) -> Result<(String, u32), Refused> {
    let streamed = resp.content_type().contains("event-stream");
    let estimate = |t: &str| (t.chars().count() / 4) as u32;
    if !streamed {
        let mut raw = Vec::new();
        resp.into_reader().take(MOST + 1).read_to_end(&mut raw).map_err(|_| Refused::other("the answer broke off"))?;
        if raw.len() as u64 > MOST {
            return Err(Refused::other(format!("the answer was over {} MB", MOST >> 20)));
        }
        let body = String::from_utf8_lossy(&raw);
        let v: Value = serde_json::from_str(&body).map_err(|_| Refused::other("its answer is not one this panel reads"))?;
        if v.get("error").is_some() {
            // (an error with the status of an answer)
            return Err(refusal(200, &body));
        }
        let text = whole(&v);
        out(Event::Chunk(text.clone()));
        return Ok((text.clone(), tokens(&v).unwrap_or(estimate(&text))));
    }
    let (mut text, mut n) = (String::new(), 0u32);
    let mut r = BufReader::new(resp.into_reader().take(MOST));
    let mut line = Vec::new();
    loop {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        line.clear();
        match r.read_until(b'\n', &mut line) {
            Ok(0) => {
                if r.get_ref().limit() == 0 {
                    out(Event::Note(format!("the answer was cut at {} MB", MOST >> 20)));
                }
                break;
            }
            Ok(_) => {}
            Err(_) if !text.is_empty() => {
                out(Event::Note("the answer stopped coming: kept as far as it came".into()));
                break;
            }
            Err(_) => return Err(Refused::other("the answer did not come")),
        }
        let line = String::from_utf8_lossy(&line);
        let Some(data) = line.trim().strip_prefix("data:") else { continue };
        let data = data.trim();
        if data == "[DONE]" {
            break;
        }
        let Ok(v) = serde_json::from_str::<Value>(data) else { continue };
        if v.get("error").is_some() {
            return Err(refusal(200, data));
        }
        let piece = pick(&v);
        if !piece.is_empty() {
            text.push_str(&piece);
            out(Event::Chunk(piece));
        }
        n = tokens(&v).unwrap_or(n);
    }
    let n = if n > 0 { n } else { estimate(&text) };
    Ok((text, n))
}

/// The answer length to ask of an address that cuts answers short when none is asked: Cloudflare's Workers AI (and
/// its AI Gateway) stops at 256 tokens by default, thinking included (gpt-oss-120b cut mid-sentence, "finish_reason":
/// "length"; 2026-11-22). Others are asked none (below).
fn length_cap(url: &str) -> Option<u32> {
    let host = url.strip_prefix("https://")?.split(['/', '?', '#', ':']).next()?.to_ascii_lowercase();
    (host == "cloudflare.com" || host.ends_with(".cloudflare.com")).then_some(4096)
}

/// OpenAI-style chat completions at `url` (no max_tokens: some models count their thinking in it, and an answer
/// cut to the board's own length would be cut in the middle; but see length_cap).
fn openai(url: &str, o: &Online, messages: &[Value], stop: &AtomicBool, out: &dyn Fn(Event)) -> Result<(String, u32), Refused> {
    if url.is_empty() {
        return Err(Refused::other("no address for its API: add it in Settings"));
    }
    if let Some(why) = prefs::address_problem(url) {
        return Err(Refused::other(why));
    }
    if o.model().is_empty() {
        return Err(Refused::other(format!("no model chosen: name one in Settings (from {})", o.provider.keys_at())));
    }
    // Brave's Answers takes one message (more: HTTP 422, "at most 1 item"; board test 2026-11-19): the question, which
    // it answers from its own search, without what was asked before
    let messages: Vec<Value> = match o.provider {
        Provider::Brave => messages.iter().rev().find(|m| m["role"] == "user").cloned().into_iter().collect(),
        _ => messages.to_vec(),
    };
    let mut payload = json!({"model": o.model(), "messages": messages, "stream": true});
    if let Some(n) = length_cap(url) {
        payload["max_tokens"] = json!(n);
    }
    let mut req = agent()
        .post(&format!("{url}/chat/completions"))
        .set("Authorization", &format!("Bearer {}", o.key))
        .set("Content-Type", "application/json");
    if o.provider == Provider::Brave {
        req = req.set("X-Subscription-Token", &o.key); // Brave's own header for its key (it takes the other too)
    }
    let resp = here(req.send_string(&payload.to_string()).map_err(unreached)?)?;
    read_answer(resp, stop, out,
                |v| v["choices"][0]["delta"]["content"].as_str().unwrap_or("").to_string(),
                |v| v["choices"][0]["message"]["content"].as_str().unwrap_or("").to_string(),
                |v| v["usage"]["completion_tokens"].as_u64().map(|n| n as u32))
}

/// Gemini's own API at `base`: the chat as its contents (the system message its instruction, "assistant" its
/// "model"), streamed, with Google Search when `search`.
fn gemini(base: &str, o: &Online, messages: &[Value], search: bool, stop: &AtomicBool, out: &dyn Fn(Event)) -> Result<(String, u32), Refused> {
    let system = messages.iter().filter(|m| m["role"] == "system").filter_map(|m| m["content"].as_str()).collect::<Vec<_>>().join("\n");
    let contents: Vec<Value> = messages.iter().filter(|m| m["role"] != "system").map(|m| {
        let role = if m["role"] == "assistant" { "model" } else { "user" };
        json!({"role": role, "parts": [{"text": m["content"].as_str().unwrap_or("")}]})
    }).collect();
    let mut payload = json!({"contents": contents, "systemInstruction": {"parts": [{"text": system}]}});
    if search {
        payload["tools"] = json!([{"google_search": {}}]);
    }
    let resp = agent()
        .post(&format!("{base}/models/{}:streamGenerateContent?alt=sse", o.model()))
        .set("x-goog-api-key", &o.key)
        .set("Content-Type", "application/json")
        .send_string(&payload.to_string())
        .map_err(unreached)?;
    let resp = here(resp)?;
    let text = |v: &Value| v["candidates"][0]["content"]["parts"].as_array().map(|parts| parts.iter()
        // its thoughts are not its answer
        .filter(|p| p["thought"].as_bool() != Some(true))
        .filter_map(|p| p["text"].as_str()).collect::<String>()).unwrap_or_default();
    read_answer(resp, stop, out, text, text, |v| v["usageMetadata"]["candidatesTokenCount"].as_u64().map(|n| n as u32))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// One answer of the fake server: its status, content type, body, and seconds to stall before the body's end
    /// (0: none).
    type Answer = (u16, &'static str, String, u64);

    /// A web server for one test: answers each request in turn; keeps what it was sent.
    struct Fake {
        url: String,
        got: Arc<Mutex<Vec<String>>>,
    }

    fn fake(answers: Vec<Answer>) -> Fake {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        let got = Arc::new(Mutex::new(Vec::new()));
        let seen = got.clone();
        std::thread::spawn(move || {
            for (code, ctype, body, stall) in answers {
                let Ok((mut s, _)) = l.accept() else { return };
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut head = String::new();
                let mut len = 0usize;
                loop {
                    let mut line = String::new();
                    if r.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(v) = line.to_lowercase().strip_prefix("content-length:") {
                        len = v.trim().parse().unwrap_or(0);
                    }
                    head.push_str(&line);
                }
                let mut b = vec![0u8; len];
                r.read_exact(&mut b).unwrap();
                seen.lock().unwrap().push(format!("{head}\n{}", String::from_utf8_lossy(&b)));
                let location = if (300..400).contains(&code) { "Location: http://127.0.0.1:9/elsewhere\r\n" } else { "" };
                if stall > 0 {
                    // half the body, then nothing (the connection open): a provider that stops sending
                    let _ = write!(s, "HTTP/1.1 {code} X\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\n\r\n{}", body.len() * 2, body);
                    std::thread::sleep(Duration::from_secs(stall));
                } else {
                    let _ = write!(s, "HTTP/1.1 {code} X\r\nContent-Type: {ctype}\r\n{location}Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                }
            }
        });
        Fake { url, got }
    }

    fn other(url: &str) -> Online {
        Online { name: "test".into(), provider: Provider::Other, model: "m1".into(), url: url.into(), key: "sk-1".into(), on: true }
    }

    fn run(f: impl FnOnce(&dyn Fn(Event))) -> (String, Vec<String>, String) {
        let (chunks, notes, end) = (Mutex::new(String::new()), Mutex::new(Vec::new()), Mutex::new(String::new()));
        f(&|ev| match ev {
            Event::Chunk(c) => chunks.lock().unwrap().push_str(&c),
            Event::Note(n) => notes.lock().unwrap().push(n),
            Event::Done { text, .. } => *end.lock().unwrap() = format!("done: {text}"),
            Event::Fail(e) => *end.lock().unwrap() = format!("fail: {e}"),
            _ => {}
        });
        (chunks.into_inner().unwrap(), notes.into_inner().unwrap(), end.into_inner().unwrap())
    }

    const SSE: &str = "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\" there\"}}],\"usage\":{\"completion_tokens\":2}}\n\ndata: [DONE]\n\n";

    #[test]
    fn an_openai_style_answer_streams_in() {
        let f = fake(vec![(200, "text/event-stream", SSE.into(), 0)]);
        let msgs = vec![json!({"role": "system", "content": "s"}), json!({"role": "user", "content": "hi"})];
        let (chunks, _, end) = run(|out| ask(&other(&f.url), &msgs, &AtomicBool::new(false), out));
        assert_eq!((chunks.as_str(), end.as_str()), ("Hello there", "done: Hello there"));
        let req = f.got.lock().unwrap()[0].clone();
        assert!(req.starts_with("POST /chat/completions"), "{req}");
        assert!(req.contains("Authorization: Bearer sk-1"), "{req}");
        assert!(!req.to_lowercase().contains("x-subscription-token"), "Brave's header to another: {req}");
        assert!(req.contains("\"model\":\"m1\"") && req.contains("\"stream\":true") && !req.contains("max_tokens"), "{req}");
    }

    #[test]
    fn cloudflare_is_asked_a_length_and_no_one_else_is() {
        let cf = "https://api.cloudflare.com/client/v4/accounts/0123456789abcdef0123456789abcdef/ai/v1";
        assert_eq!(length_cap(cf), Some(4096));
        assert_eq!(length_cap("https://gateway.ai.cloudflare.com/v1/acc/gw/workers-ai/v1"), Some(4096));
        assert_eq!(length_cap("https://API.Cloudflare.com:443/x"), Some(4096));
        for not in ["https://openrouter.ai/api/v1", "https://api.groq.com/openai/v1", "https://cloudflare.com.evil.example/v1",
                    "https://notcloudflare.com/v1", "http://127.0.0.1:8080/v1", "https://example.com/?u=api.cloudflare.com"] {
            assert_eq!(length_cap(not), None, "{not}");
        }
    }

    #[test]
    fn brave_gets_its_own_header_and_the_question_alone() {
        let f = fake(vec![(200, "text/event-stream", SSE.into(), 0),
                          (422, "application/json", r#"{"type":"ErrorResponse","error":{"status":422,"detail":"The provided API key is invalid.","code":"SUBSCRIPTION_TOKEN_INVALID"}}"#.into(), 0),
                          (422, "application/json", r#"{"type":"ErrorResponse","error":{"status":422,"detail":"Unable to validate request parameter(s)","meta":{"errors":[{"type":"too_long","loc":["body","messages"],"msg":"List should have at most 1 item after validation, not 2"}]}}}"#.into(), 0)]);
        let o = Online { provider: Provider::Brave, ..other("") };
        let msgs = [json!({"role": "system", "content": "s"}), json!({"role": "user", "content": "hi"}),
                    json!({"role": "assistant", "content": "hello"}), json!({"role": "user", "content": "and now?"})];
        // (Brave's address is its own: the requests go to the fake one through openai() directly)
        let asked = || run(|out| match openai(&f.url, &o, &msgs, &AtomicBool::new(false), out) {
            Ok(_) => out(Event::Done { text: String::new(), tok_s: 0.0, tokens: 0 }),
            Err(e) => out(Event::Fail(e.said())),
        }).2;
        assert_eq!(asked(), "done: ");
        let req = f.got.lock().unwrap()[0].clone();
        assert!(req.contains("X-Subscription-Token: sk-1"));
        let body: Value = serde_json::from_str(req.split("\n\n").last().unwrap().trim()).unwrap();
        assert_eq!(body["messages"], json!([{"role": "user", "content": "and now?"}]));
        // its refusals, in its own shape
        assert_eq!(asked(), "fail: the key was refused (HTTP 422: The provided API key is invalid.)");
        assert_eq!(asked(), "fail: the question was refused (HTTP 422: Unable to validate request parameter(s): List should have at most 1 item after validation, not 2)");
    }

    #[test]
    fn a_provider_that_does_not_stream_and_ones_that_refuse() {
        let f = fake(vec![(200, "application/json", r#"{"choices":[{"message":{"content":"Whole answer."}}]}"#.into(), 0),
                          (401, "application/json", r#"{"error":{"message":"Invalid API key"}}"#.into(), 0),
                          (429, "application/json", r#"{"error":{"message":"Quota exceeded"}}"#.into(), 0),
                          (200, "application/json", r#"{"error":{"message":"model overloaded"}}"#.into(), 0),
                          (400, "application/json", format!(r#"{{"error":{{"message":"{}"}}}}"#, "x".repeat(10_000)), 0),
                          (302, "text/plain", String::new(), 0)]);
        let msgs = vec![json!({"role": "user", "content": "hi"})];
        let o = other(&f.url);
        assert_eq!(run(|out| ask(&o, &msgs, &AtomicBool::new(false), out)).2, "done: Whole answer.");
        assert_eq!(run(|out| ask(&o, &msgs, &AtomicBool::new(false), out)).2, "fail: test: the key was refused (HTTP 401: Invalid API key)");
        assert_eq!(test(&o).unwrap_err(), "test: too many questions, or the key's quota is used up (HTTP 429: Quota exceeded)");
        assert_eq!(test(&o).unwrap_err(), "test: the question was refused (model overloaded)"); // not "no answer"
        assert!(test(&o).unwrap_err().len() < 400); // a provider's long message shortened
        let e = test(&o).unwrap_err();
        assert_eq!(e, "test: it sent the question to another address (HTTP 302): not followed, the key would go with it");
        assert_eq!(f.got.lock().unwrap().len(), 6); // the redirect was not followed (with the key)
    }

    #[test]
    fn an_answer_that_stops_coming_is_kept_as_far_as_it_came() {
        // ("Hello" arrives, then nothing for longer than the idle wait: shortened here to keep the test quick)
        let f = fake(vec![(200, "text/event-stream", "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\n".into(), 3)]);
        let resp = ureq::AgentBuilder::new().timeout_read(Duration::from_secs(1)).build()
            .post(&format!("{}/chat/completions", f.url)).send_string("{}").unwrap();
        let (chunks, notes, _) = run(|out| {
            let r = read_answer(resp, &AtomicBool::new(false), out, |v| v["choices"][0]["delta"]["content"].as_str().unwrap_or("").to_string(),
                                |_| String::new(), |_| None);
            assert_eq!(r.unwrap().0, "Hello");
        });
        assert_eq!(chunks, "Hello");
        assert_eq!(notes, ["the answer stopped coming: kept as far as it came"]);
    }

    #[test]
    fn nothing_is_sent_without_a_good_key_a_model_or_an_address() {
        let mut o = other("");
        assert!(test(&o).unwrap_err().contains("no address"));
        o.url = "http://127.0.0.1:9".into();
        o.model.clear();
        assert!(test(&o).unwrap_err().contains("no model chosen"));
        o.key = "sk-proj-SECRET\u{ad}KEY".into();
        let e = test(&o).unwrap_err();
        assert!(e.contains("not plain text") && !e.contains("SECRET"), "{e}"); // never the key itself
        o.key.clear();
        assert!(test(&o).unwrap_err().contains("has no key yet"));
    }

    #[test]
    fn gemini_gets_its_own_shape_searches_and_drops_its_thoughts() {
        let sse = "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"thinking...\",\"thought\":true},{\"text\":\"Sunny, \"}],\"role\":\"model\"}}]}\n\ndata: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"12 degrees.\"}]}}],\"usageMetadata\":{\"candidatesTokenCount\":5}}\n\n";
        let f = fake(vec![(200, "text/event-stream", sse.into(), 0)]);
        let o = Online { name: "google".into(), provider: Provider::Google, key: "k".into(), on: true, ..Default::default() };
        let msgs = vec![json!({"role": "system", "content": "be brief"}), json!({"role": "user", "content": "weather?"}),
                        json!({"role": "assistant", "content": "Where?"}), json!({"role": "user", "content": "Lisbon"})];
        let (chunks, _, end) = run(|out| ask_at(&f.url, &o, &msgs, &AtomicBool::new(false), out));
        assert_eq!((chunks.as_str(), end.as_str()), ("Sunny, 12 degrees.", "done: Sunny, 12 degrees."));
        let req = f.got.lock().unwrap()[0].clone();
        assert!(req.starts_with("POST /models/gemini-flash-latest:streamGenerateContent?alt=sse"), "{req}");
        assert!(req.to_lowercase().contains("x-goog-api-key: k"), "{req}");
        let body: Value = serde_json::from_str(req.split("\n\n").last().unwrap().trim()).unwrap();
        assert_eq!(body["systemInstruction"]["parts"][0]["text"], "be brief");
        assert_eq!(body["contents"].as_array().unwrap().iter().map(|c| c["role"].as_str().unwrap()).collect::<Vec<_>>(), ["user", "model", "user"]);
        assert_eq!(body["tools"], json!([{"google_search": {}}]));
    }

    #[test]
    fn gemini_asked_again_without_search_only_before_any_answer() {
        let answer = "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"Plain answer.\"}]}}]}\n\n";
        let f = fake(vec![(400, "application/json", r#"{"error":{"message":"Search Grounding is not supported."}}"#.into(), 0),
                          (200, "text/event-stream", answer.into(), 0)]);
        let o = Online { name: "google".into(), provider: Provider::Google, key: "k".into(), on: true, ..Default::default() };
        let msgs = vec![json!({"role": "system", "content": "s"}), json!({"role": "user", "content": "q"})];
        let (chunks, notes, end) = run(|out| ask_at(&f.url, &o, &msgs, &AtomicBool::new(false), out));
        assert_eq!((chunks.as_str(), end.as_str()), ("Plain answer.", "done: Plain answer."));
        assert!(notes[0].contains("asked without searching"), "{notes:?}");
        let got = f.got.lock().unwrap();
        assert!(got[0].contains("google_search") && !got[1].contains("google_search"));
    }

    #[test]
    fn gemini_not_asked_again_when_the_key_is_refused() {
        let f = fake(vec![(400, "application/json", r#"{"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT"}}"#.into(), 0)]);
        let o = Online { name: "google".into(), provider: Provider::Google, key: "k".into(), on: true, ..Default::default() };
        let msgs = vec![json!({"role": "user", "content": "q"})];
        let (_, notes, end) = run(|out| ask_at(&f.url, &o, &msgs, &AtomicBool::new(false), out));
        assert_eq!(end, "fail: google: the key was refused (HTTP 400: API key not valid. Please pass a valid API key.)");
        assert!(notes.is_empty(), "{notes:?}");
        assert_eq!(f.got.lock().unwrap().len(), 1);
    }

    #[test]
    fn a_provider_that_repeats_the_key_does_not_show_it() {
        let f = fake(vec![(401, "application/json", r#"{"error":{"message":"bad token sk-1-LONGKEY for this account"}}"#.into(), 0)]);
        let o = Online { key: "sk-1-LONGKEY".into(), ..other(&f.url) };
        let e = test(&o).unwrap_err();
        assert!(!e.contains("LONGKEY") && e.contains("bad token … for"), "{e}");
    }

    #[test]
    fn an_answer_over_the_most_is_said_so() {
        let big = format!(r#"{{"choices":[{{"message":{{"content":"{}"}}}}]}}"#, "x".repeat(MOST as usize + 10));
        let f = fake(vec![(200, "application/json", big, 0)]);
        assert_eq!(test(&other(&f.url)).unwrap_err(), "test: the answer was over 4 MB");
    }

    #[test]
    fn a_remote_plain_http_address_gets_no_key() {
        let e = test(&other("http://192.0.2.1/v1")).unwrap_err();
        assert!(e.contains("https://"), "{e}");
    }

    #[test]
    fn its_prompt_is_its_own() {
        let o = Online { name: "brave".into(), ..Default::default() };
        let p = system_prompt(&o);
        assert!(p.starts_with("You are brave,") && p.contains("read aloud"));
    }
}
