//! The planner used by the examples and tests: asks which language to target when the caller
//! didn't say, then returns a short plan.

use serde_json::json;

use crate::{Outcome, TaskContext};

pub const PLAN_CLASS: &str = "dev.agentbus.task.plan";
pub const PLAN_SLOT: &str = "org.agentbus.cap.Plan1";

pub async fn plan(ctx: TaskContext) -> Outcome {
    match run(&ctx).await {
        Ok(outcome) => outcome,
        Err(e) => Outcome::Failed(e.to_string()),
    }
}

async fn run(ctx: &TaskContext) -> crate::Result<Outcome> {
    let goal = ctx.spec().goal.clone();
    ctx.progress(10.0, "reading the goal").await?;
    let language = match ctx.spec().params["language"].as_str() {
        Some(l) => l.to_string(),
        None => {
            let answer = ctx
                .ask(
                    "Which language should the plan target?",
                    r#"{"type":"object","required":["language"],"properties":{"language":{"type":"string"}}}"#,
                )
                .await?;
            match answer["language"].as_str() {
                Some(l) => l.to_string(),
                None => return Ok(Outcome::Failed("answer has no language".into())),
            }
        }
    };
    ctx.progress(60.0, &format!("planning in {language}"))
        .await?;
    let steps = vec![
        format!("restate the goal: {goal}"),
        format!("sketch the {language} modules"),
        "write tests first".to_string(),
        "implement and iterate".to_string(),
    ];
    ctx.progress(100.0, "done").await?;
    Ok(Outcome::Completed(
        json!({"language": language, "steps": steps}),
    ))
}
