//! App-domain subscriptions over a real session bus. Run under `dbus-run-session`
//! (`pixi run test` does).

use std::time::Duration;

use agentbus::{
    hub,
    topics::{self, Demand},
    zbus::Connection,
};
use serde_json::json;
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(5);

#[tokio::test]
async fn subscriber_gets_only_its_subject_and_retained_value_first() {
    let hub_name = "dev.agentbus.test.TopicsA";
    let _hub = hub::serve(hub_name).await.unwrap();
    let publisher = Connection::session().await.unwrap();
    let app = Connection::session().await.unwrap();

    // Published before anyone subscribes: retained, then replayed on subscribe.
    topics::publish(
        &publisher,
        hub_name,
        "weather.current",
        "Berlin",
        &json!({"temp_c": 11.5}),
    )
    .await
    .unwrap();

    let mut sub = topics::subscribe(&app, hub_name, "weather.*", "Berlin")
        .await
        .unwrap();
    let first = timeout(WAIT, sub.next_event()).await.unwrap().unwrap();
    assert_eq!(first.topic, "weather.current");
    assert_eq!(first.payload["temp_c"], 11.5);

    let paris = topics::publish(
        &publisher,
        hub_name,
        "weather.alert",
        "Paris",
        &json!({"severity": "minor"}),
    )
    .await
    .unwrap();
    assert_eq!(paris, 0);
    let berlin = topics::publish(
        &publisher,
        hub_name,
        "weather.alert",
        "Berlin",
        &json!({"severity": "severe"}),
    )
    .await
    .unwrap();
    assert_eq!(berlin, 1);

    let next = timeout(WAIT, sub.next_event()).await.unwrap().unwrap();
    assert_eq!(next.topic, "weather.alert");
    assert_eq!(next.subject, "Berlin");
    assert_eq!(next.payload["severity"], "severe");
    assert!(next.seq > first.seq);
}

#[tokio::test]
async fn demand_follows_subscribers_and_their_lifetimes() {
    let hub_name = "dev.agentbus.test.TopicsB";
    let _hub = hub::serve(hub_name).await.unwrap();
    let source = Connection::session().await.unwrap();
    let mut demand = topics::watch_demand(&source, hub_name).await.unwrap();
    assert!(demand.current.is_empty());

    let want = |n| Demand {
        pattern: "calendar.event.changed".into(),
        subject: "work".into(),
        subscribers: n,
    };

    // An explicit unsubscribe drops demand.
    let app = Connection::session().await.unwrap();
    let sub = topics::subscribe(&app, hub_name, "calendar.event.changed", "work")
        .await
        .unwrap();
    assert_eq!(
        timeout(WAIT, demand.next_change()).await.unwrap().unwrap(),
        want(1)
    );
    sub.unsubscribe().await.unwrap();
    assert_eq!(
        timeout(WAIT, demand.next_change()).await.unwrap().unwrap(),
        want(0)
    );

    // So does the app leaving the bus without unsubscribing.
    let leaver = Connection::session().await.unwrap();
    let _sub = topics::subscribe(&leaver, hub_name, "calendar.event.changed", "work")
        .await
        .unwrap();
    assert_eq!(
        timeout(WAIT, demand.next_change()).await.unwrap().unwrap(),
        want(1)
    );
    drop(_sub);
    leaver.graceful_shutdown().await;
    assert_eq!(
        timeout(WAIT, demand.next_change()).await.unwrap().unwrap(),
        want(0)
    );
}

#[tokio::test]
async fn bad_topics_are_refused() {
    let hub_name = "dev.agentbus.test.TopicsC";
    let _hub = hub::serve(hub_name).await.unwrap();
    let conn = Connection::session().await.unwrap();
    assert!(
        topics::subscribe(&conn, hub_name, "weather.", "")
            .await
            .is_err()
    );
    assert!(
        topics::publish(&conn, hub_name, "weather.*", "", &json!({}))
            .await
            .is_err()
    );
}
