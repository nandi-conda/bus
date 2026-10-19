//! Broadcast signals over a real session bus. Run under `dbus-run-session`.

use std::time::Duration;

use agentbus::{signals, zbus::Connection};
use serde_json::json;
use tokio::time::timeout;

const PATH: &str = "/org/example/Files";
const IFACE: &str = "org.example.Files1";
const WAIT: Duration = Duration::from_secs(5);

#[tokio::test]
async fn request_reply_broadcast() {
    let app = Connection::session().await.unwrap();
    let provider = Connection::session().await.unwrap();
    let mut requests = signals::listen(&provider, PATH, IFACE, Some("GetFiles"))
        .await
        .unwrap();
    let mut replies = signals::listen(&app, PATH, IFACE, Some("SendFiles"))
        .await
        .unwrap();

    signals::broadcast(&app, PATH, IFACE, "GetFiles", &[json!("r1")])
        .await
        .unwrap();
    let req = timeout(WAIT, requests.next_signal())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (req.member.as_str(), req.args.clone()),
        ("GetFiles", vec![json!("r1")])
    );
    assert_eq!(req.sender, app.unique_name().unwrap().as_str());

    let files = json!(["a.txt", "b/c.md"]);
    signals::broadcast(
        &provider,
        PATH,
        IFACE,
        "SendFiles",
        &[json!("r1"), json!("local"), files.clone()],
    )
    .await
    .unwrap();
    let reply = timeout(WAIT, replies.next_signal()).await.unwrap().unwrap();
    assert_eq!(reply.args, vec![json!("r1"), json!("local"), files]);

    // Typed the way QtDBus and busctl expect: (s, s, as), and an empty list is still `as`.
    let mut raw = signals::listen(&app, PATH, IFACE, Some("Typed"))
        .await
        .unwrap();
    signals::broadcast(
        &provider,
        PATH,
        IFACE,
        "Typed",
        &[json!([]), json!([1, 2]), json!({"k": 1.5})],
    )
    .await
    .unwrap();
    let typed = timeout(WAIT, raw.next_signal()).await.unwrap().unwrap();
    assert_eq!(
        typed.args,
        vec![json!([]), json!([1, 2]), json!({"k": 1.5})]
    );

    let mut none = signals::listen(&app, PATH, IFACE, Some("Ping"))
        .await
        .unwrap();
    signals::broadcast(&provider, PATH, IFACE, "Ping", &[])
        .await
        .unwrap();
    assert!(
        timeout(WAIT, none.next_signal())
            .await
            .unwrap()
            .unwrap()
            .args
            .is_empty()
    );
}
