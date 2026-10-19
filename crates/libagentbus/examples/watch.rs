//! An app that watches a topic: `watch [PATTERN] [SUBJECT] [COUNT]`
//! (default `weather.*` for `Berlin`, 5 events).

use agentbus::{
    topics::{self, TOPICS_NAME},
    zbus::Connection,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let pattern = args.next().unwrap_or_else(|| "weather.*".into());
    let subject = args.next().unwrap_or_else(|| "Berlin".into());
    let count: usize = args.next().map(|n| n.parse()).transpose()?.unwrap_or(5);

    let conn = Connection::session().await?;
    let mut sub = topics::subscribe(&conn, TOPICS_NAME, &pattern, &subject).await?;
    println!("watch: subscribed to {pattern} for {subject:?}");
    for _ in 0..count {
        let e = sub.next_event().await?;
        println!(
            "watch: #{} {} [{}] {}",
            e.seq, e.topic, e.subject, e.payload
        );
    }
    sub.unsubscribe().await?;
    Ok(())
}
