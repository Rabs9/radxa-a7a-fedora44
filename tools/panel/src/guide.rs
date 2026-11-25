//! Getting started: the steps that set the board up for voice and the assistants, shown when the panel opens until
//! its owner ticks "Don't show this when the panel opens" (~/.config/a7a-panel/panel.json, {"guide_at_start": false});
//! "Getting started" beside the tabs opens it again. Every step says what is done from the board's own state (the
//! listener's status, the models on the card, the online assistants in the settings), so it is right however the
//! owner got there, and each has the buttons that do it.

use crate::{agent, prefs, ui, voice, Message, Panel, Tab};
use iced::widget::{button, checkbox, column, container, row, space, text, Column};
use iced::{Alignment, Color, Element, Fill};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// ~/.config/a7a-panel/panel.json (XDG_CONFIG_HOME when set), beside the Assistant's settings.
pub fn path() -> PathBuf {
    prefs::Prefs::path().with_file_name("panel.json")
}

/// Whether the guide opens with the panel: yes unless the file says no (missing or broken: yes).
pub fn at_start(path: &Path) -> bool {
    let v: Value = std::fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
    v["guide_at_start"].as_bool().unwrap_or(true)
}

/// Writes the choice, keeping whatever else the file holds; atomically, mode 600 like the settings beside it.
pub fn save_at_start(path: &Path, on: bool) -> Result<(), String> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut v: Value = std::fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(json!({}));
    if !v.is_object() {
        v = json!({});
    }
    v["guide_at_start"] = json!(on);
    let dir = path.parent().ok_or("no folder for the panel's settings")?;
    std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let tmp = path.with_extension("json.tmp");
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    f.write_all(format!("{v:#}\n").as_bytes()).map_err(|e| format!("{}: {e}", tmp.display()))?;
    f.sync_all().ok();
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

/// Where a step stands.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum State {
    Done,
    Working,
    ToDo,
    Optional,
    Unknown,
}

impl State {
    fn chip(self) -> (&'static str, Color) {
        match self {
            State::Done => ("done", ui::GOOD),
            State::Working => ("working", ui::NPU),
            State::ToDo => ("to do", ui::WARN),
            State::Optional => ("optional", ui::DIM),
            State::Unknown => ("…", ui::DIM),
        }
    }
}

/// The voice commands: set up, and switched on and listening.
pub fn voice_state(v: &voice::Status, known: bool) -> (State, String) {
    if !known {
        return (State::Unknown, "Asking the listener…".into());
    }
    if let Some(s) = v.setup.as_ref() {
        if !s.error.is_empty() {
            return (State::ToDo, format!("Setting up stopped: {}", s.error));
        }
        return (State::Working, format!("Setting up: {} ({:.0}%)", s.step, s.fraction * 100.0));
    }
    if !v.ready {
        return (State::ToDo, "Not set up yet: Set up downloads the speech models once (a few minutes).".into());
    }
    if !v.enabled {
        return (State::ToDo, "Set up, but switched off.".into());
    }
    if !v.error.is_empty() {
        return (State::ToDo, format!("Switched on, but: {}", v.error));
    }
    let mut phrases = vec![quoted(&v.config.wake)];
    if !v.config.assistant.is_empty() {
        phrases.push(quoted(&v.config.assistant));
    }
    phrases.extend(v.online.iter().map(|n| quoted(&format!("hey {n}"))));
    (State::Done, format!("Listening for {}.", and_list(&phrases)))
}

/// The board's own model: on the card, or coming down.
pub fn model_state(models: &[String], download: Option<f32>) -> (State, String) {
    if let Some(f) = download {
        return (State::Working, format!("Downloading the model: {:.0}% of 2.4 GB.", f * 100.0));
    }
    match models.iter().find(|m| m.as_str() == agent::DEFAULT_MODEL).or(models.first()) {
        Some(m) => (State::Done, format!("On the board: {}.", m.trim_end_matches(".gguf"))),
        None => (State::ToDo, "No model on the board yet.".into()),
    }
}

/// The online assistants that can answer (switched on, with a key), and the names the listener cannot wake for.
pub fn online_state(online: &[prefs::Online], left_out: &[String]) -> (State, String) {
    let ready: Vec<String> = online.iter().filter(|o| o.on && !o.key.trim().is_empty()).map(|o| quoted(&o.name)).collect();
    if ready.is_empty() {
        return (State::Optional, "None yet.".into());
    }
    let mut s = format!("Ready: {}.", and_list(&ready));
    if !left_out.is_empty() {
        s.push_str(&format!(" The listener cannot wake for {}: give it a name said as it is written.",
                            and_list(&left_out.iter().map(|n| quoted(&format!("hey {n}"))).collect::<Vec<_>>())));
    }
    (State::Done, s)
}

fn quoted(s: &str) -> String {
    format!("\u{201c}{s}\u{201d}")
}

fn and_list(v: &[String]) -> String {
    match v {
        [] => String::new(),
        [a] => a.clone(),
        [rest @ .., last] => format!("{} and {last}", rest.join(", ")),
    }
}

/// The cloud assistants the guide offers, each with a free way in, and what to know before choosing it. Its Add
/// button opens the settings with a row of that provider, named as `name()` says.
pub const OPTIONS: [(prefs::Provider, &str); 5] = [
    (prefs::Provider::Cloudflare, "Free with an account: about 350 answers a day. It needs your Account ID and an API token (step by step below). Tested on this board."),
    (prefs::Provider::Google, "Gemini: a free key at aistudio.google.com, with a Google account. It searches Google for its answers."),
    (prefs::Provider::Groq, "A free key at console.groq.com; very fast. Type the model's name from its list (console.groq.com/docs/models), for example llama-3.3-70b-versatile."),
    (prefs::Provider::OpenRouter, "openrouter.ai: hundreds of models, some free (their names end in :free; its privacy settings must allow them)."),
    (prefs::Provider::Mistral, "console.mistral.ai: a free plan for trying it (Experiment)."),
];

/// The name a provider's new row gets: what one says after "hey". Cloudflare's is short ("hey cloud"); a name
/// taken already is numbered when the settings are saved ("second google").
pub fn name(p: prefs::Provider) -> String {
    match p {
        prefs::Provider::Cloudflare => "cloud".into(),
        p => p.key().into(),
    }
}

/// Cloudflare, step by step: the one tested on the board (2026-11-22).
const CLOUD_STEPS: [&str; 4] = [
    "Sign up at dash.cloudflare.com: an e-mail address is enough, no card.",
    "In the dashboard, open AI, then Workers AI, and choose Use REST API: it shows your Account ID, and its button makes an API token for Workers AI. Copy both.",
    "Here: Add beside Cloudflare above, paste the Account ID and the token into the new row, press Test, then Save.",
    "Its name is how you call it: \u{201c}cloud\u{201d} answers \u{201c}hey cloud, \u{2026}\u{201d}. Any name works if it is said the way it is written: \u{201c}cloud\u{201d} and \u{201c}nova\u{201d} do, \u{201c}qwen\u{201d} does not (the listener cannot hear it).",
];

impl Panel {
    pub(crate) fn guide_view(&self) -> Element<'_, Message> {
        let wake = if self.voice.config.wake.is_empty() { "hey cubie".to_string() } else { self.voice.config.wake.clone() };
        let assistant = if self.voice.config.assistant.is_empty() { "hey assistant".to_string() } else { self.voice.config.assistant.clone() };
        let busy = self.voice_busy;

        let (vs, vtext) = voice_state(&self.voice, self.voice_known || self.demo);
        let mut vbuttons = row![].spacing(8);
        if !self.voice.ready && self.voice.setup.as_ref().is_none_or(|s| !s.error.is_empty()) {
            vbuttons = vbuttons.push(primary("Set up voice", (!busy).then_some(Message::VoiceSetup)));
        } else if self.voice.ready && !self.voice.enabled {
            vbuttons = vbuttons.push(primary("Turn on", (!busy).then_some(Message::VoiceOn(true))));
        }
        vbuttons = vbuttons.push(crate::small_button("Open the Voice tab", Message::GuideGo(Tab::Voice)));
        let voice = step(
            1,
            "Talk to your Cubie",
            vs,
            vtext,
            column![
                para("Plug a USB microphone into the board. What you say is heard on the board itself: nothing leaves it, except a question you put to a cloud assistant (step 3)."),
                para(format!("Then say \u{201c}{wake}, volume up\u{201d} or \u{201c}{wake}, open firefox\u{201d}. Typing works too: \u{201c}{wake}, type hello world\u{201d}, \u{201c}{wake}, go to lemonde dot fr\u{201d}. The Voice tab lists every command, lets you add your own and change the wake phrase.")),
            ]
            .spacing(6)
            .into(),
            vbuttons.into(),
        );

        let (ms, mtext) = model_state(&self.models, self.download);
        let mut mbuttons = row![].spacing(8);
        if self.models.is_empty() && self.download.is_none() {
            mbuttons = mbuttons.push(primary("Download the model (2.4 GB)", Some(Message::LlmGet)));
        }
        mbuttons = mbuttons.push(crate::small_button("Open the Assistant", Message::GuideGo(Tab::Assistant)));
        let model = step(
            2,
            "The board's own assistant",
            ms,
            mtext,
            column![
                para("A language model that runs on this board: it works without the internet and your questions stay here, but it is slow (about two words a second). It can read the board's temperatures, its memory, the folders you allow, and the web if you let it."),
                para(format!("Ask it in the Assistant tab, or say \u{201c}{assistant}, how hot is the board?\u{201d} The answer comes as a notification, and can be read aloud.")),
            ]
            .spacing(6)
            .into(),
            mbuttons.into(),
        );

        let (os, otext) = online_state(&self.prefs.online, &self.voice.left_out);
        let mut options = Column::new().spacing(6);
        for (p, about) in OPTIONS {
            options = options.push(
                row![
                    text(p.to_string()).size(13).font(ui::SEMIBOLD).width(200),
                    text(about).size(12).color(ui::DIM).width(Fill),
                    crate::small_button("Add", Message::GuideAddOnline(p)),
                ]
                .spacing(10)
                .align_y(Alignment::Center),
            );
        }
        let mut numbered = Column::new().spacing(4);
        for (i, s) in CLOUD_STEPS.iter().enumerate() {
            numbered = numbered.push(row![text(format!("{}.", i + 1)).size(13).width(18).color(ui::NPU), text(*s).size(13).width(Fill)].spacing(6));
        }
        let cloud = step(
            3,
            "A cloud assistant (free)",
            os,
            otext,
            column![
                para("A much stronger model on the internet. What you ask it goes to that company with your key; it does not see this board. Each of these has a free way in:"),
                options,
                text("Cloudflare, step by step").size(13).font(ui::SEMIBOLD),
                numbered,
            ]
            .spacing(8)
            .into(),
            row![crate::small_button("Open the cloud settings", Message::GuideSettings)].into(),
        );

        let settings = step(
            4,
            "Settings worth a look",
            State::Optional,
            String::new(),
            column![
                bullet("Your time zone: the board starts on UTC. Set yours in System Settings (search for \u{201c}Date & Time\u{201d}), so the clock, the weather and the assistants\u{2019} \u{201c}today\u{201d} are yours."),
                bullet("Speak answers: the switch beside web tools in the Assistant tab reads answers aloud, in a voice you pick."),
                bullet("Web tools: the board's assistant may search the web, open a page and look up the weather. One chat uses either this board's files or the web, never both."),
                bullet("Folders: which folders it may read. Instructions: how it answers (shorter, in French, \u{2026})."),
                bullet("Your own commands: in the Voice tab, a phrase and the command it runs."),
            ]
            .spacing(4)
            .into(),
            row![
                crate::small_button("Open the Assistant's settings", Message::GuideSettings),
                crate::small_button("Open the Voice tab", Message::GuideGo(Tab::Voice)),
            ]
            .spacing(8)
            .into(),
        );

        // the box and Close at the top: the board's own screen (1024x600) shows only the first step or two
        let mut top = row![
            text("Getting started").size(22).font(ui::BOLD),
            space::horizontal(),
            checkbox(!self.guide_at_start).label("Don't show this when the panel opens").on_toggle(|b| Message::GuideAtStart(!b)).text_size(13),
            button(text("Close").size(13).font(ui::SEMIBOLD)).padding([8, 18]).style(ui::pill(ui::NPU, true)).on_press(Message::Guide(false)),
        ]
        .spacing(14)
        .align_y(Alignment::Center);
        if !self.guide_error.is_empty() {
            top = top.push(text(self.guide_error.as_str()).size(12).color(ui::HOT));
        }

        let mut page = Column::new().spacing(14).padding([4, 0]).push(
            column![
                top,
                text("Your Cubie takes spoken commands, answers questions with a model that runs on the board itself, and can ask a cloud assistant for harder ones. Each step says whether it is done; do them now or later. Getting started, beside the tabs, brings this back.")
                    .size(13)
                    .color(ui::DIM),
            ]
            .spacing(4),
        );
        // the headphone jack, first, only when it needs something (a board built with the AC101, or a switch made
        // and not yet restarted): spoken answers come out of it
        if self.jack.wants_action() {
            page = page.push(
                container(column![text("Headphone jack").size(16).font(ui::SEMIBOLD), self.jack_view()].spacing(10))
                    .padding(14)
                    .width(Fill)
                    .style(ui::tool_box),
            );
        }
        page.push(voice).push(model).push(cloud).push(settings).into()
    }
}

fn para<'a>(s: impl text::IntoFragment<'a>) -> Element<'a, Message> {
    text(s).size(13).into()
}

fn bullet(s: &str) -> Element<'_, Message> {
    row![text("\u{2022}").size(13).color(ui::NPU), text(s).size(13).width(Fill)].spacing(8).into()
}

fn primary(label: &str, on: Option<Message>) -> Element<'_, Message> {
    button(text(label).size(13).font(ui::SEMIBOLD)).padding([7, 16]).style(ui::pill(ui::NPU, true)).on_press_maybe(on).into()
}

fn step<'a>(n: usize, title: &'a str, state: State, status: String, body: Element<'a, Message>, buttons: Element<'a, Message>) -> Element<'a, Message> {
    let (word, color) = state.chip();
    let mut head = row![
        text(format!("{n}")).size(15).font(ui::BOLD).color(ui::NPU).width(22),
        text(title).size(16).font(ui::SEMIBOLD),
        space::horizontal(),
        crate::chip(word.to_string(), color),
    ]
    .spacing(8)
    .align_y(Alignment::Center);
    head = head.width(Fill);
    let mut col = column![head, body].spacing(10);
    if !status.is_empty() {
        col = col.push(text(status).size(12).color(if state == State::Done { ui::GOOD } else { ui::DIM }));
    }
    col = col.push(buttons);
    container(col).padding(14).width(Fill).style(ui::tool_box).into()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn status(ready: bool, enabled: bool) -> voice::Status {
        let mut s = voice::demo();
        s.ready = ready;
        s.enabled = enabled;
        s.running = enabled;
        s.error.clear();
        s.setup = None;
        s.config.wake = "hey cubie".into();
        s.config.assistant = "hey assistant".into();
        s.online = vec!["cloud".into()];
        s
    }

    #[test]
    fn the_voice_step_from_the_listeners_state() {
        assert_eq!(voice_state(&status(true, true), false).0, State::Unknown);
        let (st, said) = voice_state(&status(true, true), true);
        assert_eq!(st, State::Done);
        assert_eq!(said, "Listening for \u{201c}hey cubie\u{201d}, \u{201c}hey assistant\u{201d} and \u{201c}hey cloud\u{201d}.");
        assert_eq!(voice_state(&status(false, false), true).0, State::ToDo);
        assert_eq!(voice_state(&status(true, false), true).1, "Set up, but switched off.");
        let mut s = status(false, false);
        s.setup = Some(voice::Setup { fraction: 0.4, step: "downloading".into(), error: String::new() });
        assert_eq!(voice_state(&s, true), (State::Working, "Setting up: downloading (40%)".into()));
        s.setup.as_mut().unwrap().error = "no network".into();
        assert_eq!(voice_state(&s, true).0, State::ToDo);
        let mut e = status(true, true);
        e.error = "no microphone".into();
        assert_eq!(voice_state(&e, true).1, "Switched on, but: no microphone");
    }

    #[test]
    fn the_model_step() {
        assert_eq!(model_state(&[], None).0, State::ToDo);
        assert_eq!(model_state(&[], Some(0.25)), (State::Working, "Downloading the model: 25% of 2.4 GB.".into()));
        let (st, said) = model_state(&["other.gguf".into(), agent::DEFAULT_MODEL.into()], None);
        assert_eq!((st, said.as_str()), (State::Done, "On the board: Qwen3-4B-Instruct-2507-Q4_0."));
    }

    #[test]
    fn the_cloud_step_and_a_name_the_listener_cannot_hear() {
        let o = |name: &str, key: &str, on: bool| prefs::Online { name: name.into(), key: key.into(), on, ..Default::default() };
        assert_eq!(online_state(&[], &[]).0, State::Optional);
        assert_eq!(online_state(&[o("cloud", "", true), o("nova", "k", false)], &[]).0, State::Optional);
        let (st, said) = online_state(&[o("cloud", "k", true), o("qwen", "k", true)], &["qwen".into()]);
        assert_eq!(st, State::Done);
        assert!(said.starts_with("Ready: \u{201c}cloud\u{201d} and \u{201c}qwen\u{201d}."), "{said}");
        assert!(said.contains("cannot wake for \u{201c}hey qwen\u{201d}"), "{said}");
    }

    #[test]
    fn the_cloud_choices() {
        // the owner asked for Google and Groq beside Cloudflare (2026-11-24); Cloudflare first: the one tested
        let offered: Vec<prefs::Provider> = OPTIONS.iter().map(|(p, _)| *p).collect();
        assert_eq!(offered[0], prefs::Provider::Cloudflare);
        for p in [prefs::Provider::Google, prefs::Provider::Groq] {
            assert!(offered.contains(&p), "{p:?}");
        }
        // each a name the settings keep as it is (prefs::online_name), the one said after "hey"
        for (p, about) in OPTIONS {
            assert_eq!(prefs::online_name(&name(p)), name(p));
            assert!(!about.is_empty());
        }
        assert_eq!(name(prefs::Provider::Cloudflare), "cloud");
        assert_eq!(name(prefs::Provider::Groq), "groq");
    }

    #[test]
    fn shown_until_the_box_is_ticked_and_the_file_keeps_the_rest() {
        let d = std::env::temp_dir().join(format!("a7a-guide-{}", std::process::id()));
        let f = d.join("a7a-panel/panel.json");
        let _ = std::fs::remove_dir_all(&d);
        assert!(at_start(&f));                                  // no file: shown
        save_at_start(&f, false).unwrap();
        assert!(!at_start(&f));
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&f).unwrap().permissions().mode() & 0o777, 0o600);
        std::fs::write(&f, "{\"other\": 1, \"guide_at_start\": false}").unwrap();
        save_at_start(&f, true).unwrap();
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        assert_eq!((v["other"].as_i64(), v["guide_at_start"].as_bool()), (Some(1), Some(true)));
        std::fs::write(&f, "{broken").unwrap();
        assert!(at_start(&f));                                  // a broken file: shown, and Save writes a good one
        save_at_start(&f, false).unwrap();
        assert!(!at_start(&f));
        let _ = std::fs::remove_dir_all(&d);
    }
}
