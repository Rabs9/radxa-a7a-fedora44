//! The Voice tab's side of a7a-voice (tools/board/a7a-voice), the board's listener: a wake phrase, then one of the
//! commands the owner types in the tab. The tab keeps no file of its own. It reads the listener's state and settings
//! from `a7a-voice status` and changes them through the program (save, test, on, off, setup), so the settings file,
//! the listener's report and the setup's progress each have one owner, and the two sides cannot disagree about a path
//! or a format. The Assistant tab's answers read aloud go through it the same way (speak, say, hush).

use crate::sys;
use serde_json::{json, Map, Value};

const PROGRAM: &str = "a7a-voice";
/// What the tab shows when it cannot ask the listener at all.
pub const NOT_INSTALLED: &str = "a7a-voice is not installed (package a7a-board-tools)";

/// One command: the words said after the wake phrase, and the command line that then runs, as the owner.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Command {
    pub say: String,
    pub run: String,
}

/// The groups the Voice tab lists the found commands under (their kind), in its order, with their headings; a kind
/// not here goes under "Other".
pub const GROUPS: &[(&str, &str)] = &[
    ("keys", "Typing keys"),
    ("browser", "The browser"),
    ("music", "Music"),
    ("window", "Windows and the desktop"),
    ("board", "The board"),
    ("system", "Keys, the window, music and the board"),
];

/// One command a scan found (`a7a-voice scan`): its id (by which the next scan knows it), the words said (the owner's
/// to change), the command line (the scan's), switched on or off, and what it is ("app": opening or closing one of the
/// menu's applications; else its group, GROUPS: keys, browser, music, window, board; "system" from a scan before the
/// groups, until the next one).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Found {
    pub id: String,
    pub say: String,
    pub run: String,
    pub on: bool,
    pub kind: String,
    /// the words the scan made (the next scan replaces words nobody changed, when the application's name changed)
    pub made: String,
}


/// The settings the tab edits: the wake phrase, the Assistant's (empty: none), and the commands in the order typed.
/// `extra` holds the rest as the listener reported it (how close a sentence must come to a command, the wake phrases'
/// threshold, the seconds a command may follow one, the microphone): settings the tab does not show, written back
/// unchanged by a Save.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Config {
    pub wake: String,
    pub assistant: String,
    pub commands: Vec<Command>,
    /// the found ones, in the scan's order
    pub found: Vec<Found>,
    pub extra: Map<String, Value>,
}

impl Config {
    /// Tolerant: a value that is not an object, or a command that is not one, gives nothing rather than an error.
    pub fn from_json(v: &Value) -> Config {
        let mut extra = v.as_object().cloned().unwrap_or_default();
        let wake = extra.remove("wake").and_then(|w| w.as_str().map(str::to_string)).unwrap_or_default();
        let assistant = extra.remove("assistant").and_then(|w| w.as_str().map(str::to_string)).unwrap_or_default();
        let commands = match extra.remove("commands") {
            Some(Value::Array(list)) => list
                .iter()
                .filter_map(Value::as_object)
                .map(|c| Command {
                    say: c.get("say").and_then(Value::as_str).unwrap_or_default().to_string(),
                    run: c.get("run").and_then(Value::as_str).unwrap_or_default().to_string(),
                })
                .collect(),
            _ => Vec::new(),
        };
        let found = match extra.remove("found") {
            Some(Value::Array(list)) => list
                .iter()
                .filter_map(Value::as_object)
                .filter_map(|f| {
                    let s = |k: &str| f.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
                    let id = s("id");
                    (!id.is_empty()).then(|| Found {
                        id,
                        say: s("say"),
                        run: s("run"),
                        on: f.get("on").and_then(Value::as_bool) != Some(false),
                        kind: s("kind"),
                        made: s("made"),
                    })
                })
                .collect(),
            _ => Vec::new(),
        };
        Config { wake, assistant, commands, found, extra }
    }

    /// The words said for more than one command that can run (the owner's, and the found ones switched on), each once,
    /// in the order first met: only the first of those runs. Compared as the listener compares them: lower case,
    /// letters and digits only, a hyphen between letters dropped ("wi-fi" is "wifi").
    pub fn said_twice(&self) -> Vec<String> {
        let norm = |s: &str| {
            let c: Vec<char> = s.to_lowercase().chars().collect();
            let joined: String = c.iter().enumerate().filter(|&(i, &ch)| !(ch == '-' && i > 0 && c[i - 1].is_alphabetic() && c.get(i + 1).is_some_and(|n| n.is_alphabetic()))).map(|(_, &ch)| ch).collect();
            joined.split(|ch: char| !ch.is_alphanumeric() && ch != '\'').filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
        };
        let said = self.commands.iter().filter(|c| !c.run.trim().is_empty()).map(|c| norm(&c.say))
            .chain(self.found.iter().filter(|f| f.on && !f.run.trim().is_empty()).map(|f| norm(&f.say)));
        let mut seen = std::collections::HashSet::new();
        let mut twice = Vec::new();
        for w in said.filter(|w| !w.is_empty()) {
            if !seen.insert(w.clone()) && !twice.contains(&w) {
                twice.push(w);
            }
        }
        twice
    }

    /// What `a7a-voice save` takes on its stdin.
    pub fn to_json(&self) -> Value {
        let mut m = self.extra.clone();
        m.insert("wake".into(), json!(self.wake));
        m.insert("assistant".into(), json!(self.assistant));
        m.insert("commands".into(), Value::Array(self.commands.iter().map(|c| json!({"say": c.say, "run": c.run})).collect()));
        m.insert(
            "found".into(),
            Value::Array(self.found.iter().map(|f| json!({"id": f.id, "say": f.say, "run": f.run, "on": f.on, "kind": f.kind, "made": f.made})).collect()),
        );
        Value::Object(m)
    }
}

/// `a7a-voice setup` at work: how far it is and what it does, or why it stopped (its progress file, read by status).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Setup {
    pub fraction: f32,
    pub step: String,
    pub error: String,
}

impl Setup {
    /// None when `v` is not an object (no setup at work).
    fn from_json(v: &Value) -> Option<Setup> {
        v.as_object().map(|p| Setup {
            fraction: p.get("fraction").and_then(Value::as_f64).unwrap_or(0.0).clamp(0.0, 1.0) as f32,
            step: p.get("step").and_then(Value::as_str).unwrap_or_default().to_string(),
            error: p.get("error").and_then(Value::as_str).unwrap_or_default().to_string(),
        })
    }
}

/// What `a7a-voice status` prints: set up or not, the service running and enabled at log-in, the listener's own report
/// while it runs (its state, the last sentence it acted on, the command that ran), the settings, a setup at work.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Status {
    pub ready: bool,
    pub running: bool,
    pub enabled: bool,
    /// "starting", "listening", "awake" (the wake phrase was heard) or "error"; empty while the listener is off
    pub state: String,
    pub heard: String,
    pub ran: String,
    /// why the listener stopped or cannot start, or why the tab could not ask it
    pub error: String,
    pub score: Option<f32>,
    /// when the listener last reported, in seconds since 1970
    pub at: Option<f64>,
    pub config: Config,
    pub config_error: String,
    pub setup: Option<Setup>,
    /// the online assistants the listener also wakes for ("hey <name>"; the panel's settings, the ones it can tell
    /// apart while it runs), and why not for the others
    pub online: Vec<String>,
    pub left_out: Vec<String>,
    /// the two phrases the listener wakes for (commands, the Assistant), when it says: beside the saved ones in
    /// `config`, a refused phrase differs (it keeps the old one; 2026-11-24)
    pub listening: Option<(String, String)>,
}

/// Why a saved phrase is not working: the listener did not take it and kept another (`listening`, what it wakes for).
/// None when they are the same phrase, or when the listener has not said.
pub fn refused(saved: &str, listening: Option<&str>) -> Option<String> {
    let heard = listening?;
    let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase();
    if norm(saved) == norm(heard) {
        return None;
    }
    Some(if heard.trim().is_empty() {
        "the listener did not take this phrase (why: above)".to_string()
    } else {
        format!("the listener did not take this phrase and still wakes for \u{201c}{}\u{201d} (why: above)", heard.trim())
    })
}

impl Status {
    /// Tolerant: what is missing or of another type takes its default; text that is not JSON gives the defaults.
    pub fn parse(s: &str) -> Status {
        let v: Value = serde_json::from_str(s).unwrap_or(Value::Null);
        let text = |k: &str| v[k].as_str().unwrap_or_default().to_string();
        let flag = |k: &str| v[k].as_bool().unwrap_or(false);
        let strings = |k: &str| v[k].as_array().map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default();
        let setup = Setup::from_json(&v["setup"]);
        Status {
            ready: flag("ready"),
            running: flag("running"),
            enabled: flag("enabled"),
            state: text("state"),
            heard: text("heard"),
            ran: text("ran"),
            error: text("error"),
            score: v["score"].as_f64().map(|x| x as f32),
            at: v["at"].as_f64(),
            config: Config::from_json(&v["config"]),
            config_error: text("config_error"),
            setup,
            online: strings("online"),
            left_out: strings("left_out"),
            listening: match (v["listening"]["wake"].as_str(), v["listening"]["assistant"].as_str()) {
                (Some(w), Some(a)) => Some((w.to_string(), a.to_string())),
                _ => None,
            },
        }
    }

    /// No answer from the listener: why, as its error.
    pub fn missing(why: &str) -> Status {
        Status { error: why.to_string(), ..Default::default() }
    }
}

/// The listener's state and settings. Blocking (a Python start, about 0.1 s on the board): through the panel's worker.
pub fn status() -> Status {
    match std::process::Command::new(PROGRAM).arg("status").stdin(std::process::Stdio::null()).output() {
        Ok(o) if o.status.success() => Status::parse(&String::from_utf8_lossy(&o.stdout)),
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr);
            Status::missing(&format!("a7a-voice status failed: {}", err.trim().lines().last().unwrap_or("no message")))
        }
        Err(_) => Status::missing(NOT_INSTALLED),
    }
}

/// Writes the settings (the listener checks them and takes them within a second); its answer, for the panel's log.
pub fn save(cfg: &Config) -> String {
    sys::command_with_input(PROGRAM, &["save"], &cfg.to_json().to_string())
}

/// `a7a-voice save`'s answer when the settings were written.
pub fn saved(out: &str) -> bool {
    out.starts_with("saved to ") && !out.contains("\n(exit ")
}

/// The command `say` matches in the saved settings, run now: the matching and the command line, both tested. One
/// that presses keys or acts on the window is only said (`--panel`): it would act on the panel itself.
pub fn test(say: &str) -> String {
    sys::command(PROGRAM, &["test", "--panel", say])
}

/// The menu's applications and the common commands found again (what the owner changed of each found one stays).
pub fn scan() -> String {
    sys::command(PROGRAM, &["scan"])
}

/// The listener now and at every log-in.
pub fn on() -> String {
    sys::command(PROGRAM, &["on"])
}

pub fn off() -> String {
    sys::command(PROGRAM, &["off"])
}

/// The speech engine and the two models, about 110 MB: a minute or two, its progress coming in through `status`.
pub fn setup() -> String {
    sys::command(PROGRAM, &["setup"])
}

/// What `a7a-voice speak` prints: the Assistant's answers read aloud or not, the voice that reads them and the ones
/// there are, the voice set up, something being read now, the voice's download at work (or why it stopped).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Speech {
    pub speak: bool,
    pub voice: String,
    pub voices: Vec<String>,
    pub ready: bool,
    pub speaking: bool,
    pub setup: Option<Setup>,
    /// a setup of either part at work (one of the Voice tab's may make a new venv, and check the voice in it last)
    pub busy: bool,
    /// why the tab could not ask
    pub error: String,
}

impl Speech {
    /// Tolerant, as Status::parse.
    pub fn parse(s: &str) -> Speech {
        let v: Value = serde_json::from_str(s).unwrap_or(Value::Null);
        let flag = |k: &str| v[k].as_bool().unwrap_or(false);
        Speech {
            speak: flag("speak"),
            voice: v["voice"].as_str().unwrap_or_default().to_string(),
            voices: v["voices"].as_array().map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default(),
            ready: flag("ready"),
            speaking: flag("speaking"),
            setup: Setup::from_json(&v["setup"]),
            busy: flag("busy"),
            error: String::new(),
        }
    }
}

/// One of the voices, shown by its name: Supertonic's F1 to F5 and M1 to M5 as "Female 1" to "Male 5".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Voice(pub String);

impl std::fmt::Display for Voice {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match (self.0.get(..1), self.0.get(1..)) {
            (Some("F"), Some(n)) => write!(f, "Female {n}"),
            (Some("M"), Some(n)) => write!(f, "Male {n}"),
            _ => write!(f, "{}", self.0),
        }
    }
}

/// Whether answers are read aloud, and how far the voice's download is. Blocking (a Python start): through the
/// panel's worker.
pub fn speech() -> Speech {
    match std::process::Command::new(PROGRAM).arg("speak").stdin(std::process::Stdio::null()).output() {
        Ok(o) if o.status.success() => Speech::parse(&String::from_utf8_lossy(&o.stdout)),
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr);
            Speech { error: format!("a7a-voice speak failed: {}", err.trim().lines().last().unwrap_or("no message")), ..Default::default() }
        }
        Err(_) => Speech { error: NOT_INSTALLED.into(), ..Default::default() },
    }
}

/// Answers read aloud from now on, or not. The first "on" downloads the voice (about 130 MB: a minute or two, its
/// progress coming in through `speech`); "on" reads a sample in the voice.
pub fn speak(on: bool) -> String {
    sys::command(PROGRAM, &["speak", if on { "on" } else { "off" }])
}

/// The voice that reads them, by its code (F1 … M5); a sample read in it.
pub fn speak_voice(code: &str) -> String {
    sys::command(PROGRAM, &["speak", "voice", code])
}

/// `text` (markdown) read aloud now, in place of whatever is being read: returns when it has been read, or stopped.
pub fn say(text: &str) -> String {
    sys::command_with_input(PROGRAM, &["say"], text)
}

/// Stops whatever is being read: an answer of this tab's or one to "hey assistant".
pub fn hush() -> String {
    sys::command(PROGRAM, &["hush"])
}

/// `say`'s answer when it did not read the text, for a reason worth showing: its last line before the exit status
/// (a traceback's error too). Not when it was stopped: ended by a signal, "(exit -1)".
pub fn unread(out: &str) -> Option<&str> {
    let status = out.lines().last().and_then(|l| l.strip_prefix("(exit ")).and_then(|c| c.trim_end_matches(')').parse::<i32>().ok());
    if !(status.is_some_and(|c| c > 0) || out.contains("could not be started")) {
        return None;
    }
    out.lines().filter(|l| !l.trim().is_empty() && !l.starts_with("(exit ")).last()
}

/// The listener's defaults (DEFAULTS in tools/board/a7a-voice; a test keeps the two the same), for --demo.
const DEFAULT_WAKE: &str = "hey cubie";
const DEFAULT_ASSISTANT: &str = "hey assistant";
const DEFAULT_COMMANDS: [(&str, &str); 7] = [
    ("volume up", "wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 10%+"),
    ("volume down", "wpctl set-volume @DEFAULT_AUDIO_SINK@ 10%-"),
    ("mute", "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"),
    ("open firefox", "firefox"),
    ("open the panel", "a7a-panel"),
    (
        "how hot is it",
        r#"notify-send -a Voice "Temperature" "$(sort -n /sys/class/thermal/thermal_zone*/temp | tail -n 1 | awk '{printf "%.0f °C", $1/1000}')""#,
    ),
    ("lock the screen", "loginctl lock-session"),
];

/// Answers read aloud, in the first voice, for --demo.
pub fn speech_demo() -> Speech {
    let voices = ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"].map(String::from).to_vec();
    Speech { speak: true, voice: voices[0].clone(), voices, ready: true, ..Default::default() }
}

/// A listener set up, running, that has just run a command.
pub fn demo() -> Status {
    let mut extra = Map::new();
    extra.insert("match".into(), json!(0.75));
    extra.insert("sensitivity".into(), json!(0.25));
    extra.insert("window".into(), json!(8.0));
    extra.insert("mic".into(), json!(""));
    Status {
        ready: true,
        running: true,
        enabled: true,
        state: "listening".into(),
        heard: "Hey computer, open Firefox.".into(),
        ran: "open firefox".into(),
        score: Some(1.0),
        online: vec!["cloud".into(), "nova".into()],
        config: Config {
            wake: DEFAULT_WAKE.into(),
            assistant: DEFAULT_ASSISTANT.into(),
            commands: DEFAULT_COMMANDS.iter().map(|(s, r)| Command { say: s.to_string(), run: r.to_string() }).collect(),
            found: [
                ("system:press-enter", "press enter", "a7a-voice key enter", true, "keys"),
                ("system:copy", "copy that", "a7a-voice key ctrl+c", true, "keys"),
                ("system:address-bar", "address bar", "a7a-voice key ctrl+l", true, "browser"),
                ("system:new-tab", "new tab", "a7a-voice key ctrl+t", true, "browser"),
                ("system:play", "play", "a7a-voice key playpause", true, "music"),
                ("system:next-song", "next song", "a7a-voice key nextsong", true, "music"),
                ("system:show-desktop", "show the desktop", "a7a-voice shortcut 'Show Desktop'", true, "window"),
                ("system:screenshot", "take a screenshot", "spectacle -b -f", true, "window"),
                ("system:time", "what time is it", "notify-send -a Voice \"$(date '+%-I:%M %p')\"", true, "board"),
                ("system:shut-down", "shut down the computer", "systemctl poweroff", false, "board"),
                ("open:org.kde.dolphin", "open dolphin", "a7a-voice app open org.kde.dolphin", true, "app"),
                ("close:org.kde.dolphin", "close dolphin", "a7a-voice app close org.kde.dolphin", true, "app"),
            ]
            .iter()
            .map(|(id, say, run, on, kind)| Found { id: id.to_string(), say: say.to_string(), run: run.to_string(), on: *on, kind: kind.to_string(), made: say.to_string() })
            .collect(),
            extra,
        },
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_group_a_scan_gives_has_a_heading() {
        // a7a-voice's SYSTEM: (id, words, command line, on at first, group); the tab lists each group under its heading
        let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../board/a7a-voice")).unwrap();
        let block = &src[src.find("\nSYSTEM = [").unwrap()..];
        let block = &block[..block.find("\n]").unwrap()];
        let mut n = 0;
        for line in block.lines().filter(|l| l.trim_start().starts_with("(\"")) {
            let group = line.trim_end().trim_end_matches("),").rsplit(", ").next().unwrap().trim_matches('"');
            assert!(GROUPS.iter().any(|(g, _)| *g == group), "the group {group} has no heading");
            n += 1;
        }
        assert!(n > 20, "{n} entries read");
    }

    const FULL: &str = r#"{"ready": true, "running": true, "enabled": true, "config": {"wake": "hey computer", "assistant": "hey assistant", "commands": [{"say": "open firefox", "run": "firefox"}, {"say": "mute", "run": "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"}], "match": 0.75, "sensitivity": 0.25, "window": 8.0, "mic": ""}, "config_error": "", "setup": null, "state": "listening", "heard": "Hey computer, open Firefox.", "ran": "open firefox", "score": 1.0, "at": 1795041972.7620754, "error": ""}"#;

    #[test]
    fn a_full_status_parses() {
        let s = Status::parse(FULL);
        assert!(s.ready && s.running && s.enabled);
        assert_eq!((s.state.as_str(), s.heard.as_str(), s.ran.as_str()), ("listening", "Hey computer, open Firefox.", "open firefox"));
        assert_eq!((s.score, s.at.map(|a| a.floor())), (Some(1.0), Some(1795041972.0)));
        assert_eq!((s.config.wake.as_str(), s.config.assistant.as_str()), ("hey computer", "hey assistant"));
        assert_eq!(s.config.commands[1], Command { say: "mute".into(), run: "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle".into() });
        assert_eq!(s.config.extra.keys().collect::<Vec<_>>(), ["match", "mic", "sensitivity", "window"]);
        assert_eq!(s.setup, None);
        // a setup at work, and one that stopped
        let s = Status::parse(r#"{"ready": false, "setup": {"fraction": 0.42, "step": "downloading the models"}}"#);
        assert_eq!(s.setup, Some(Setup { fraction: 0.42, step: "downloading the models".into(), error: String::new() }));
        let s = Status::parse(r#"{"setup": {"fraction": 0.0, "step": "", "error": "no network"}, "error": "not set up"}"#);
        assert_eq!((s.setup.unwrap().error.as_str(), s.error.as_str()), ("no network", "not set up"));
    }

    #[test]
    fn garbage_gives_the_defaults() {
        assert_eq!(Status::parse("not json"), Status::default());
        assert_eq!(Status::parse(r#"{"listening": {"wake": "hey cubie", "assistant": "hey assistant"}}"#).listening,
                   Some(("hey cubie".into(), "hey assistant".into())));
        assert_eq!(Status::parse(r#"{"listening": {"wake": "hey cubie"}}"#).listening, None);
        assert_eq!(Status::parse("[1, 2]"), Status::default());
        assert_eq!(Status::parse(""), Status::default());
        let s = Status::parse(r#"{"ready": "yes", "state": 3, "config": 5, "score": "x", "setup": {"fraction": "half", "step": 7}}"#);
        assert_eq!((s.ready, s.state.as_str(), s.score), (false, "", None));
        assert_eq!(s.config, Config::default());
        assert_eq!(s.setup, Some(Setup::default()));
        let c = Config::from_json(&json!({"wake": 1, "commands": [{"say": "a"}, 5, {"run": "b"}]}));
        assert_eq!((c.wake.as_str(), c.commands.len()), ("", 2));
        assert_eq!(c.commands[1], Command { say: String::new(), run: "b".into() });
    }

    #[test]
    fn a_config_round_trip_keeps_the_rest_and_the_order() {
        let mut c = Status::parse(FULL).config;
        c.commands.push(Command { say: "zebra".into(), run: "z".into() });
        c.commands.insert(0, Command { say: "aardvark".into(), run: "a".into() });
        c.wake = "okay computer".into();
        c.assistant = String::new();                       // none: written as an empty phrase, not left out
        let back = Config::from_json(&c.to_json());
        assert_eq!(back, c);
        assert_eq!(c.to_json()["assistant"].as_str(), Some(""));
        let says: Vec<&str> = back.commands.iter().map(|x| x.say.as_str()).collect();
        assert_eq!(says, ["aardvark", "open firefox", "mute", "zebra"]);
        let j = c.to_json();
        assert_eq!((j["match"].as_f64(), j["window"].as_f64(), j["mic"].as_str()), (Some(0.75), Some(8.0), Some("")));
    }

    #[test]
    fn found_commands_round_trip_and_the_words_said_twice() {
        let v: Value = serde_json::from_str(r#"{"wake": "hey cubie", "commands": [{"say": "Open Firefox", "run": "firefox"}, {"say": "x", "run": " "}],
            "found": [{"id": "open:firefox", "say": "open firefox", "run": "a7a-voice app open firefox", "on": true, "kind": "app"},
                      {"id": "system:play", "say": "play", "run": "a7a-voice key playpause", "kind": "system"},
                      {"id": "system:pause", "say": "x", "run": "y", "on": false, "kind": "system"},
                      {"say": "no id"}, 3]}"#).unwrap();
        let c = Config::from_json(&v);
        assert_eq!(c.found.iter().map(|f| (f.id.as_str(), f.on)).collect::<Vec<_>>(), [("open:firefox", true), ("system:play", true), ("system:pause", false)]);
        assert_eq!(c.found[1].kind, "system");
        assert!(!c.extra.contains_key("found"));
        assert_eq!(Config::from_json(&c.to_json()), c);
        // the built-in "open firefox" and the found one, both able to run; "x" twice, but one of them has nothing to run
        // and the other is off
        assert_eq!(c.said_twice(), ["open firefox"]);
        let mut c2 = c.clone();
        c2.commands.push(Command { say: "Turn Wi-Fi off".into(), run: "nmcli radio wifi off".into() });
        c2.found.push(Found { id: "w".into(), say: "turn wifi off".into(), run: "x".into(), on: true, ..Default::default() });
        assert_eq!(c2.said_twice(), ["open firefox", "turn wifi off"]);
    }

    #[test]
    fn speech_parses_and_names_its_voices() {
        let s = Speech::parse(r#"{"speak": true, "voice": "M2", "voices": ["F1", "F2", "M1", "M2"], "ready": true, "speaking": false, "setup": null, "busy": true}"#);
        assert!(s.speak && s.ready && !s.speaking && s.busy);
        assert_eq!((s.voice.as_str(), s.voices.len(), s.setup), ("M2", 4, None));
        let s = Speech::parse(r#"{"speak": false, "setup": {"fraction": 0.5, "step": "downloading the voice"}}"#);
        assert_eq!(s.setup, Some(Setup { fraction: 0.5, step: "downloading the voice".into(), error: String::new() }));
        assert_eq!(Speech::parse("not json"), Speech::default());
        assert_eq!(Speech::parse(r#"{"speak": "yes", "voices": [1, "F1"]}"#).voices, ["F1"]);
        assert_eq!(Voice("F3".into()).to_string(), "Female 3");
        assert_eq!(Voice("M5".into()).to_string(), "Male 5");
        assert_eq!((Voice("X".into()).to_string(), Voice(String::new()).to_string()), ("X".into(), String::new()));
    }

    #[test]
    fn unread_tells_a_failure_from_a_stop() {
        assert_eq!(unread("not read aloud: the player stopped (exit 3): no sound card\n(exit 1)"), Some("not read aloud: the player stopped (exit 3): no sound card"));
        assert!(unread("the voice is not set up: turn on speak answers in the panel's Assistant tab\n(exit 1)").is_some());
        assert_eq!(unread("\n(exit -1)"), None); // stopped by hush: ended by a signal, nothing to show
        assert_eq!(unread(""), None);
        assert_eq!(unread("Traceback (most recent call last):\n  File \"x\", line 1\nValueError: embedded null byte\n(exit 1)"), Some("ValueError: embedded null byte"));
        assert_eq!(unread("not read aloud: what is being read does not stop\n(exit 1)"), Some("not read aloud: what is being read does not stop"));
        assert!(unread("a7a-voice could not be started: No such file or directory (os error 2)").is_some());
    }

    #[test]
    fn saved_reads_the_answer() {
        assert!(saved("saved to /home/radxa/.config/a7a-voice/config.json"));
        assert!(!saved("not saved: Expecting value\n(exit 1)"));
        assert!(!saved("a7a-voice could not be started: No such file or directory (os error 2)"));
    }

    #[test]
    fn the_demo_is_the_listeners_defaults() {
        // DEFAULTS as Python reads it: string pieces on following lines joined, \" unescaped
        let script = include_str!("../../board/a7a-voice");
        let start = script.find("DEFAULTS = {").expect("DEFAULTS in a7a-voice");
        let block = &script[start..start + script[start..].find("\n}\n").expect("the end of DEFAULTS")];
        let joined = regex::Regex::new(r#""\s*\n\s*""#).unwrap().replace_all(block, "").replace("\\\"", "\"");
        assert!(joined.contains(&format!("\"wake\": \"{DEFAULT_WAKE}\"")), "{joined}");
        assert!(joined.contains(&format!("\"assistant\": \"{DEFAULT_ASSISTANT}\"")), "{joined}");
        for (say, run) in DEFAULT_COMMANDS {
            let entry = format!("{{\"say\": \"{say}\", \"run\": \"{run}\"}}");
            assert!(joined.contains(&entry), "not in a7a-voice's DEFAULTS: {entry}");
        }
        assert_eq!(joined.matches("{\"say\": ").count(), DEFAULT_COMMANDS.len());
        // and the settings the tab does not show, as the demo carries them
        for (k, v) in demo().config.extra {
            assert!(joined.contains(&format!("\"{k}\": {v}")), "{k}: {v}");
        }
    }

    #[test]
    fn a_refused_phrase_is_told_from_the_one_it_wakes_for() {
        // the same phrase (spacing and case aside), or a listener that has not said: nothing to warn of
        assert_eq!(refused("hey cubie", Some("hey cubie")), None);
        assert_eq!(refused("Hey  Cubie", Some("hey cubie")), None);
        assert_eq!(refused("hey qwen", None), None);
        // saved "hey qwen", refused, the listener still on "hey nova": the row says so
        let n = refused("hey qwen", Some("hey nova")).unwrap();
        assert!(n.contains("did not take this phrase") && n.contains("\u{201c}hey nova\u{201d}"), "{n}");
        assert!(refused("hey qwen", Some("")).unwrap().contains("did not take"));
    }
}
