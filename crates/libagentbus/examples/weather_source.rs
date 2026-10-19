//! A weather service that publishes `weather.current` (and the odd `weather.alert`) only for the
//! places someone is watching. It starts a feed when demand for a place appears and stops it when
//! the last subscriber leaves. The readings are made up; a real source would call a weather API.

use std::{collections::HashMap, time::Duration};

use agentbus::{
    topics::{self, Demand, TOPICS_NAME, pattern_matches},
    zbus::Connection,
};
use serde_json::json;
use tokio::task::JoinHandle;

fn covers_weather(d: &Demand) -> bool {
    pattern_matches(&d.pattern, "weather.current") || pattern_matches(&d.pattern, "weather.alert")
}

fn feed(conn: Connection, place: String) -> JoinHandle<()> {
    tokio::spawn(async move {
        let base = place.bytes().map(f64::from).sum::<f64>() % 20.0;
        for tick in 0u32.. {
            let temp = base + f64::from(tick % 5);
            let reading =
                json!({"temp_c": temp, "condition": "cloudy", "source": "weather_source"});
            let _ = topics::publish(&conn, TOPICS_NAME, "weather.current", &place, &reading).await;
            if tick % 3 == 2 {
                let alert =
                    json!({"severity": "minor", "headline": format!("Gusty wind in {place}")});
                let _ = topics::publish(&conn, TOPICS_NAME, "weather.alert", &place, &alert).await;
            }
            tokio::time::sleep(Duration::from_millis(700)).await;
        }
    })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let conn = Connection::session().await?;
    let mut demand = topics::watch_demand(&conn, TOPICS_NAME).await?;
    // Subscribers per place, summed over every pattern that covers weather topics.
    let mut watchers: HashMap<(String, String), u32> = HashMap::new();
    let mut feeds: HashMap<String, JoinHandle<()>> = HashMap::new();
    println!("weather_source: waiting for subscribers");

    let mut pending: Vec<Demand> = std::mem::take(&mut demand.current);
    loop {
        if pending.is_empty() {
            pending.push(demand.next_change().await?);
        }
        for d in pending.drain(..) {
            // Subjects are places; "" (any place) gives us nothing to poll.
            if !covers_weather(&d) || d.subject.is_empty() {
                continue;
            }
            watchers.insert((d.pattern.clone(), d.subject.clone()), d.subscribers);
            let total: u32 = watchers
                .iter()
                .filter(|((_, s), _)| *s == d.subject)
                .map(|(_, n)| n)
                .sum();
            match (total, feeds.contains_key(&d.subject)) {
                (0, true) => {
                    feeds.remove(&d.subject).unwrap().abort();
                    println!(
                        "weather_source: nobody watches {} any more, stopping",
                        d.subject
                    );
                }
                (n, false) if n > 0 => {
                    println!("weather_source: {} is watched, starting feed", d.subject);
                    feeds.insert(d.subject.clone(), feed(conn.clone(), d.subject.clone()));
                }
                _ => {}
            }
        }
    }
}
