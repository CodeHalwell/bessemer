//! Axum side of the head-to-head. Routes match the two Python apps.
//! Usage: axum-compare <port> <worker threads>

use axum::{
    Json, Router,
    extract::{Path, Query},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Duration;

#[derive(Deserialize, Serialize)]
struct Item {
    id: i64,
    name: String,
    price: f64,
    tags: Vec<String>,
}

#[derive(Deserialize, Serialize)]
struct Order {
    items: Vec<Item>,
}

#[derive(Deserialize)]
struct Search {
    q: Option<String>,
}

async fn item(Path(item_id): Path<i64>, Query(search): Query<Search>) -> Json<Value> {
    Json(json!({"id": item_id, "q": search.q.unwrap_or_else(|| "x".into())}))
}

fn main() {
    let mut args = std::env::args().skip(1);
    let port: u16 = args.next().unwrap().parse().unwrap();
    let threads: usize = args.next().unwrap().parse().unwrap();
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads)
        .enable_all()
        .build()
        .unwrap()
        .block_on(async move {
            let app = Router::new()
                .route("/healthz", get(|| async { Json(json!({"ok": true})) }))
                .route("/json", get(|| async { Json(json!({"ok": true, "count": 42})) }))
                .route("/items/{item_id}", get(item))
                .route("/echo", post(|Json(order): Json<Order>| async { Json(order) }))
                .route(
                    "/io",
                    get(|| async {
                        tokio::time::sleep(Duration::from_millis(1)).await;
                        Json(json!({"ok": true}))
                    }),
                );
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await.unwrap();
            axum::serve(listener, app).await.unwrap();
        });
}
