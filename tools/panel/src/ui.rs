//! Colours, card and button styles, and the two drawn widgets: the sparkline (a minute of a value) and the bar.

use iced::widget::canvas::{self, Frame, Geometry, Path, Stroke};
use iced::widget::{button, container, text_input};
use iced::{border, mouse, Background, Border, Color, Font, Point, Rectangle, Renderer, Shadow, Theme};
use std::collections::VecDeque;

pub const BG: Color = rgb(0x0b0e14);
pub const CARD: Color = rgb(0x131822);
pub const CARD_HI: Color = rgb(0x1a2130);
pub const LINE: Color = rgb(0x252e3f);
pub const TEXT: Color = rgb(0xe8ebf2);
pub const DIM: Color = rgb(0x8a94a8);

pub const BIG: Color = rgb(0x4cc9f0);
pub const LITTLE: Color = rgb(0x5a8dee);
pub const GPU: Color = rgb(0xf04d9b);
pub const RAM: Color = rgb(0xffb703);
pub const NPU: Color = rgb(0xb388ff);
pub const DSU: Color = rgb(0x2ec4b6);
pub const GOOD: Color = rgb(0x5ec26a);
pub const WARN: Color = rgb(0xf4a259);
pub const HOT: Color = rgb(0xef476f);

pub const BOLD: Font = Font { weight: iced::font::Weight::Bold, ..Font::DEFAULT };
pub const SEMIBOLD: Font = Font { weight: iced::font::Weight::Semibold, ..Font::DEFAULT };

const fn rgb(v: u32) -> Color {
    Color::from_rgb(((v >> 16) & 0xff) as f32 / 255.0, ((v >> 8) & 0xff) as f32 / 255.0, (v & 0xff) as f32 / 255.0)
}

pub fn alpha(c: Color, a: f32) -> Color {
    Color { a, ..c }
}

pub fn theme() -> Theme {
    Theme::custom(
        "A7A",
        iced::theme::Palette { background: BG, text: TEXT, primary: BIG, success: GOOD, warning: WARN, danger: HOT },
    )
}

/// A temperature's colour: green to 55 °C, amber to 70, red above.
pub fn temp_color(t: f32) -> Color {
    if t < 55.0 {
        GOOD
    } else if t < 70.0 {
        WARN
    } else {
        HOT
    }
}

pub fn card(_: &Theme) -> container::Style {
    container::Style {
        text_color: Some(TEXT),
        background: Some(Background::Color(CARD)),
        border: Border { color: LINE, width: 1.0, radius: border::radius(14) },
        shadow: Shadow { color: alpha(Color::BLACK, 0.35), offset: iced::Vector::new(0.0, 3.0), blur_radius: 12.0 },
        snap: true,
    }
}

pub fn header(_: &Theme) -> container::Style {
    container::Style {
        text_color: Some(TEXT),
        background: Some(Background::Gradient(
            iced::gradient::Linear::new(iced::Radians(0.0))
                .add_stop(0.0, rgb(0x101622))
                .add_stop(1.0, rgb(0x161d2c))
                .into(),
        )),
        border: Border { color: LINE, width: 1.0, radius: border::radius(0) },
        shadow: Shadow::default(),
        snap: true,
    }
}

/// A small rounded label with a coloured edge.
pub fn chip(c: Color) -> impl Fn(&Theme) -> container::Style {
    move |_| container::Style {
        text_color: Some(c),
        background: Some(Background::Color(alpha(c, 0.12))),
        border: Border { color: alpha(c, 0.55), width: 1.0, radius: border::radius(999) },
        shadow: Shadow::default(),
        snap: true,
    }
}

/// Buttons: `selected` for the setting in effect (filled with the accent), else an outline that lights up on hover.
pub fn pill(accent: Color, selected: bool) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let hover = matches!(status, button::Status::Hovered | button::Status::Pressed);
        let (bg, fg, edge) = if selected {
            (accent, BG, accent)
        } else if hover {
            (alpha(accent, 0.22), TEXT, accent)
        } else {
            (CARD_HI, TEXT, LINE)
        };
        button::Style {
            background: Some(Background::Color(if matches!(status, button::Status::Disabled) { CARD } else { bg })),
            text_color: if matches!(status, button::Status::Disabled) { DIM } else { fg },
            border: Border { color: edge, width: 1.0, radius: border::radius(999) },
            shadow: Shadow::default(),
            snap: true,
        }
    }
}

/// The user's side of the chat.
pub fn bubble(_: &Theme) -> container::Style {
    container::Style {
        text_color: Some(TEXT),
        background: Some(Background::Color(alpha(BIG, 0.14))),
        border: Border { color: alpha(BIG, 0.45), width: 1.0, radius: border::radius(14) },
        shadow: Shadow::default(),
        snap: true,
    }
}

/// A tool the model used, under its answer: quieter than the answer itself.
pub fn tool_box(_: &Theme) -> container::Style {
    container::Style {
        text_color: Some(TEXT),
        background: Some(Background::Color(alpha(CARD, 0.6))),
        border: Border { color: alpha(DSU, 0.35), width: 1.0, radius: border::radius(10) },
        shadow: Shadow::default(),
        snap: true,
    }
}

pub fn input(_: &Theme, status: text_input::Status) -> text_input::Style {
    let edge = match status {
        text_input::Status::Focused { .. } => BIG,
        text_input::Status::Hovered => alpha(BIG, 0.5),
        _ => LINE,
    };
    text_input::Style {
        background: Background::Color(CARD),
        border: Border { color: edge, width: 1.0, radius: border::radius(12) },
        icon: DIM,
        placeholder: DIM,
        value: TEXT,
        selection: alpha(BIG, 0.35),
    }
}

pub fn tab(selected: bool) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let hover = matches!(status, button::Status::Hovered | button::Status::Pressed);
        button::Style {
            background: Some(Background::Color(if selected { alpha(BIG, 0.16) } else if hover { CARD_HI } else { Color::TRANSPARENT })),
            text_color: if selected { BIG } else { TEXT },
            border: Border { color: if selected { alpha(BIG, 0.6) } else { Color::TRANSPARENT }, width: 1.0, radius: border::radius(10) },
            shadow: Shadow::default(),
            snap: true,
        }
    }
}

/// A minute of one value, drawn as a line over a fading fill. Values are 0..1.
pub struct Sparkline<'a> {
    pub values: &'a VecDeque<f32>,
    pub color: Color,
    pub capacity: usize,
}

impl<'a, Message> canvas::Program<Message> for Sparkline<'a> {
    type State = ();

    fn draw(&self, _: &(), renderer: &Renderer, _: &Theme, bounds: Rectangle, _: mouse::Cursor) -> Vec<Geometry> {
        let mut frame = Frame::new(renderer, bounds.size());
        let (w, h) = (bounds.width, bounds.height);
        // faint guide lines at a quarter, half and three quarters
        for f in [0.25f32, 0.5, 0.75] {
            let y = h * f;
            frame.stroke(&Path::line(Point::new(0.0, y), Point::new(w, y)), Stroke::default().with_width(1.0).with_color(alpha(LINE, 0.6)));
        }
        if self.values.len() >= 2 {
            let step = w / (self.capacity.max(2) - 1) as f32;
            let x0 = w - step * (self.values.len() - 1) as f32;
            let pt = |i: usize, v: f32| Point::new(x0 + step * i as f32, h - 2.0 - v.clamp(0.0, 1.0) * (h - 4.0));
            let line = Path::new(|b| {
                for (i, v) in self.values.iter().enumerate() {
                    if i == 0 { b.move_to(pt(i, *v)) } else { b.line_to(pt(i, *v)) }
                }
            });
            let area = Path::new(|b| {
                b.move_to(Point::new(x0, h));
                for (i, v) in self.values.iter().enumerate() {
                    b.line_to(pt(i, *v));
                }
                b.line_to(Point::new(w, h));
                b.close();
            });
            frame.fill(
                &area,
                canvas::Fill::from(
                    canvas::gradient::Linear::new(Point::new(0.0, 0.0), Point::new(0.0, h))
                        .add_stop(0.0, alpha(self.color, 0.35))
                        .add_stop(1.0, alpha(self.color, 0.02)),
                ),
            );
            frame.stroke(&line, Stroke::default().with_width(2.0).with_color(self.color));
            if let Some(v) = self.values.back() {
                let p = pt(self.values.len() - 1, *v);
                frame.fill(&Path::circle(p, 3.5), self.color);
            }
        }
        vec![frame.into_geometry()]
    }
}

/// A horizontal bar, `value` 0..1, with a rounded track.
pub struct Bar {
    pub value: f32,
    pub color: Color,
}

impl<Message> canvas::Program<Message> for Bar {
    type State = ();

    fn draw(&self, _: &(), renderer: &Renderer, _: &Theme, bounds: Rectangle, _: mouse::Cursor) -> Vec<Geometry> {
        let mut frame = Frame::new(renderer, bounds.size());
        let (w, h) = (bounds.width, bounds.height);
        let r = h / 2.0;
        frame.fill(&Path::rounded_rectangle(Point::ORIGIN, iced::Size::new(w, h), r.into()), CARD_HI);
        let fw = (w * self.value.clamp(0.0, 1.0)).max(h);
        frame.fill(
            &Path::rounded_rectangle(Point::ORIGIN, iced::Size::new(fw, h), r.into()),
            canvas::Fill::from(
                canvas::gradient::Linear::new(Point::new(0.0, 0.0), Point::new(fw, 0.0))
                    .add_stop(0.0, alpha(self.color, 0.55))
                    .add_stop(1.0, self.color),
            ),
        );
        vec![frame.into_geometry()]
    }
}
