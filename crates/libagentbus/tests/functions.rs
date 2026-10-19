//! Functions over a real session bus. Run under `dbus-run-session` (`pixi run test` does).

use std::{collections::HashMap, time::Duration};

use agentbus::{
    Error,
    functions::{self, Procs, function},
    hub,
    zbus::Connection,
};
use serde_json::json;

const SCHEMA: &str = r#"{"args": {"type": "object", "required": ["place"]},
                         "result": {"type": "object", "required": ["temp_c"]}}"#;

async fn weather(hub: &str, temp: f64) -> Procs {
    let procs = Procs::connect(hub).await.unwrap();
    procs
        .provide(
            "weather.current",
            SCHEMA,
            function(
                move |args, _| async move { Ok(json!({"temp_c": temp, "place": args["place"]})) },
            ),
        )
        .await
        .unwrap();
    procs
}

fn code(e: Error) -> String {
    match e {
        Error::Call { name, .. } => name,
        other => panic!("expected a call error, got {other}"),
    }
}

#[tokio::test]
async fn call_validates_and_fails_over() {
    let hub_name = "dev.agentbus.test.FunctionsA";
    let _hub = hub::serve_with_pins(hub_name, HashMap::new())
        .await
        .unwrap();
    let app = Connection::session().await.unwrap();

    let err = functions::call(
        &app,
        hub_name,
        "weather.current",
        &json!({"place": "Berlin"}),
        None,
    )
    .await;
    assert_eq!(
        code(err.unwrap_err()),
        "org.freedesktop.DBus.Error.ServiceUnknown"
    );

    // A broken provider first, then a good one: the hub fails over.
    let broken = Procs::connect(hub_name).await.unwrap();
    broken
        .provide(
            "weather.current",
            SCHEMA,
            function(|_, _| async { Err("upstream down".to_string()) }),
        )
        .await
        .unwrap();
    let good = weather(hub_name, 11.5).await;
    for _ in 0..3 {
        let r = functions::call(
            &app,
            hub_name,
            "weather.current",
            &json!({"place": "Berlin"}),
            None,
        )
        .await
        .unwrap();
        assert_eq!(r, json!({"temp_c": 11.5, "place": "Berlin"}));
    }
    let stats = functions::providers(&app, hub_name, "weather.current")
        .await
        .unwrap();
    assert_eq!(stats.len(), 2);
    assert!(
        stats.iter().any(|(_, s)| s["failures"] == 1),
        "broken one tried once, then ranked last: {stats:?}"
    );

    let err = functions::call(
        &app,
        hub_name,
        "weather.current",
        &json!({"city": "Berlin"}),
        None,
    )
    .await;
    assert_eq!(
        code(err.unwrap_err()),
        "org.freedesktop.DBus.Error.InvalidArgs"
    );

    // Leaving the bus ends the registration.
    drop(good);
    drop(broken);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        functions::functions(&app, hub_name)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn malformed_results_are_never_delivered() {
    let hub_name = "dev.agentbus.test.FunctionsB";
    let _hub = hub::serve_with_pins(hub_name, HashMap::new())
        .await
        .unwrap();
    let liar = Procs::connect(hub_name).await.unwrap();
    liar.provide(
        "weather.current",
        SCHEMA,
        function(|_, _| async { Ok(json!({"temperature": 3})) }),
    )
    .await
    .unwrap();
    let app = Connection::session().await.unwrap();
    let err = functions::call(
        &app,
        hub_name,
        "weather.current",
        &json!({"place": "x"}),
        None,
    )
    .await;
    assert_eq!(code(err.unwrap_err()), "org.freedesktop.DBus.Error.Failed");
}

#[tokio::test]
async fn pins_win_and_timeouts_cancel() {
    let hub_name = "dev.agentbus.test.FunctionsC";
    let pinned = Procs::connect(hub_name).await.unwrap();
    pinned
        .connection()
        .request_name("dev.agentbus.test.PinnedWeather")
        .await
        .unwrap();
    let pins = HashMap::from([(
        "weather.current".to_string(),
        "dev.agentbus.test.PinnedWeather".to_string(),
    )]);
    let _hub = hub::serve_with_pins(hub_name, pins).await.unwrap();
    let _other = weather(hub_name, 1.0).await;
    pinned
        .provide(
            "weather.current",
            SCHEMA,
            function(|_, _| async { Ok(json!({"temp_c": 99.0})) }),
        )
        .await
        .unwrap();
    let app = Connection::session().await.unwrap();
    for _ in 0..3 {
        let r = functions::call(
            &app,
            hub_name,
            "weather.current",
            &json!({"place": "x"}),
            None,
        )
        .await
        .unwrap();
        assert_eq!(r["temp_c"], 99.0);
    }

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let slow = Procs::connect(hub_name).await.unwrap();
    slow.provide(
        "build.run",
        "",
        function(move |_, info| {
            let tx = tx.clone();
            async move {
                info.cancelled().await;
                let _ = tx.send(info.call_id.clone());
                Err("canceled".to_string())
            }
        }),
    )
    .await
    .unwrap();
    let err = functions::call(
        &app,
        hub_name,
        "build.run",
        &json!({}),
        Some(Duration::from_millis(200)),
    )
    .await;
    assert_eq!(code(err.unwrap_err()), "org.freedesktop.DBus.Error.Timeout");
    let canceled = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap();
    assert!(canceled.is_some());
}

#[tokio::test]
async fn providers_only_answer_the_hub() {
    let hub_name = "dev.agentbus.test.FunctionsD";
    let _hub = hub::serve_with_pins(hub_name, HashMap::new())
        .await
        .unwrap();
    let procs = weather(hub_name, 5.0).await;
    let intruder = Connection::session().await.unwrap();
    let owner = procs.connection().unique_name().unwrap().to_string();
    let direct = intruder
        .call_method(
            Some(owner.as_str()),
            functions::PROVIDER_PATH,
            Some(functions::PROVIDER_IFACE),
            "Invoke",
            &(
                "weather.current",
                agentbus::json_to_dict(&json!({"place": "x"})).unwrap(),
                agentbus::Dict::new(),
            ),
        )
        .await;
    assert!(direct.is_err());
}

#[tokio::test]
async fn call_one_named_provider() {
    let hub_name = "dev.agentbus.test.FunctionsProvider";
    let _hub = hub::serve_with_pins(hub_name, HashMap::new())
        .await
        .unwrap();
    let app = Connection::session().await.unwrap();
    let cold = weather(hub_name, 3.0).await;
    let warm = weather(hub_name, 21.0).await;
    let args = json!({"place": "Berlin"});

    // Fan out: every provider answers for itself, with no failover between them.
    for (procs, temp) in [(&cold, 3.0), (&warm, 21.0)] {
        let owner = procs.connection().unique_name().unwrap().to_string();
        let r =
            functions::call_provider(&app, hub_name, "weather.current", Some(&owner), &args, None)
                .await
                .unwrap();
        assert_eq!(r["temp_c"], temp);
    }

    let app_name = app.unique_name().unwrap().to_string();
    let err = functions::call_provider(
        &app,
        hub_name,
        "weather.current",
        Some(&app_name),
        &args,
        None,
    )
    .await;
    assert_eq!(
        code(err.unwrap_err()),
        "org.freedesktop.DBus.Error.ServiceUnknown"
    );
}
