//! Submit a planning task to the `org.agentbus.cap.Plan1` slot, answer its question, print
//! the deliverable.

use agentbus::{
    TaskEvent, TaskSpec, TaskState,
    demo::{PLAN_CLASS, PLAN_SLOT},
    submit,
    zbus::Connection,
};
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let conn = Connection::session().await?;
    let spec = TaskSpec::new(PLAN_CLASS, "build a tiny key-value store");
    let mut task = submit(&conn, PLAN_SLOT, &spec).await?;
    println!("sender: submitted {}", task.handle().path());
    while let Some(event) = task.next_event().await? {
        match event {
            TaskEvent::Progress { pct, note } => println!("sender: progress {pct:>5.1}% {note}"),
            TaskEvent::Log { level, msg } => println!("sender: log [{level}] {msg}"),
            TaskEvent::Question { id, text, .. } => {
                println!("sender: question {id}: {text} -> rust");
                task.handle()
                    .respond(&id, &json!({"language": "rust"}))
                    .await?;
            }
            TaskEvent::Finished(state) => println!("sender: finished {}", state.as_str()),
        }
    }
    let result = task.handle().result().await?;
    println!("sender: deliverable {}", result.deliverable);
    if result.state != TaskState::Completed {
        return Err(format!("task ended {}: {}", result.state.as_str(), result.report).into());
    }
    Ok(())
}
