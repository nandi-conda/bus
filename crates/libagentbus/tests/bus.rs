//! End-to-end tests over a real session bus. Run them under `dbus-run-session`
//! (`pixi run test` does).

use std::time::Duration;

use agentbus::{
    Agent, Outcome, TaskEvent, TaskSpec, TaskState,
    demo::{PLAN_CLASS, plan},
    handler, router, submit,
    zbus::Connection,
};
use serde_json::json;

async fn planner_behind(slot: &str, name: &str) -> (Agent, Agent) {
    let router = router::serve(slot).await.unwrap();
    let agent = Agent::serve(name, vec![PLAN_CLASS.into()], handler(plan))
        .await
        .unwrap();
    agent.join_slot(slot).await.unwrap();
    (router, agent)
}

#[tokio::test]
async fn routed_task_asks_and_completes() {
    let slot = "org.agentbus.test.Plan1";
    let _bus = planner_behind(slot, "org.agentbus.test.planner").await;
    let conn = Connection::session().await.unwrap();
    let mut task = submit(&conn, slot, &TaskSpec::new(PLAN_CLASS, "a kv store"))
        .await
        .unwrap();

    let mut progress = Vec::new();
    let mut asked = false;
    while let Some(event) = task.next_event().await.unwrap() {
        match event {
            TaskEvent::Progress { pct, .. } => progress.push(pct),
            TaskEvent::Question { id, .. } => {
                assert_eq!(
                    task.handle().state().await.unwrap(),
                    TaskState::InputRequired
                );
                task.handle()
                    .respond(&id, &json!({"language": "zig"}))
                    .await
                    .unwrap();
                asked = true;
            }
            TaskEvent::Log { .. } => {}
            TaskEvent::Finished(state) => assert_eq!(state, TaskState::Completed),
        }
    }
    assert!(asked);
    assert_eq!(progress, vec![10.0, 60.0, 100.0]);
    let result = task.handle().result().await.unwrap();
    assert_eq!(result.state, TaskState::Completed);
    assert_eq!(result.deliverable["language"], "zig");
    assert_eq!(result.deliverable["steps"].as_array().unwrap().len(), 4);
}

#[tokio::test]
async fn cancel_while_waiting_for_an_answer() {
    let slot = "org.agentbus.test.Cancel1";
    let _bus = planner_behind(slot, "org.agentbus.test.cancelee").await;
    let conn = Connection::session().await.unwrap();
    let mut task = submit(&conn, slot, &TaskSpec::new(PLAN_CLASS, "never answered"))
        .await
        .unwrap();
    loop {
        if let Some(TaskEvent::Question { .. }) = task.next_event().await.unwrap() {
            break;
        }
    }
    task.handle().cancel("changed my mind").await.unwrap();
    let mut last = None;
    while let Some(event) = task.next_event().await.unwrap() {
        last = Some(event);
    }
    assert_eq!(last, Some(TaskEvent::Finished(TaskState::Canceled)));
    let result = task.handle().result().await.unwrap();
    assert_eq!(result.report["reason"], "changed my mind");
}

#[tokio::test]
async fn empty_slot_rejects() {
    let slot = "org.agentbus.test.Empty1";
    let _router = router::serve(slot).await.unwrap();
    let conn = Connection::session().await.unwrap();
    let mut task = submit(&conn, slot, &TaskSpec::new(PLAN_CLASS, "nobody home"))
        .await
        .unwrap();
    while task.next_event().await.unwrap().is_some() {}
    assert_eq!(
        task.handle().result().await.unwrap().state,
        TaskState::Rejected
    );
}

#[tokio::test]
async fn direct_submit_failure_and_bad_spec() {
    let name = "org.agentbus.test.failer";
    let _agent = Agent::serve(
        name,
        vec!["dev.agentbus.task.fail".into()],
        handler(|_ctx| async { Outcome::Failed("on purpose".into()) }),
    )
    .await
    .unwrap();
    let conn = Connection::session().await.unwrap();

    // Wrong class is refused at Submit.
    assert!(
        submit(&conn, name, &TaskSpec::new(PLAN_CLASS, "x"))
            .await
            .is_err()
    );

    let mut task = submit(
        &conn,
        name,
        &TaskSpec::new("dev.agentbus.task.fail", "fail"),
    )
    .await
    .unwrap();
    let finished = tokio::time::timeout(Duration::from_secs(5), async {
        let mut last = None;
        while let Some(e) = task.next_event().await.unwrap() {
            last = Some(e);
        }
        last
    })
    .await
    .unwrap();
    assert_eq!(finished, Some(TaskEvent::Finished(TaskState::Failed)));
    assert_eq!(
        task.handle().result().await.unwrap().report["reason"],
        "on purpose"
    );
}
