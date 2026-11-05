use axum::{Router, extract::State, routing::get};
use pyo3::{
    exceptions::{PyRuntimeError, PyStopIteration},
    prelude::*,
    types::PyModule,
};
use serde::Serialize;
use std::{env, net::SocketAddr, sync::Arc};
use tokio::net::TcpListener;

#[derive(Debug, PartialEq, Serialize)]
struct Payload {
    ok: bool,
    count: u64,
}

async fn rust_handler() -> axum::Json<Payload> {
    axum::Json(Payload {
        ok: true,
        count: 42,
    })
}

fn python_function() -> PyResult<Py<PyAny>> {
    Python::attach(|py| {
        let module = PyModule::from_code(
            py,
            c"async def handler():\n    return {'ok': True, 'count': 42}\n",
            c"handler.py",
            c"handler",
        )?;
        Ok(module.getattr("handler")?.unbind())
    })
}

fn call_python(py: Python<'_>, handler: &Py<PyAny>) -> PyResult<Payload> {
    let coroutine = handler.bind(py).call0()?;
    let result = match coroutine.call_method1("send", (py.None(),)) {
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
            error.value(py).getattr("value")?
        }
        Err(error) => return Err(error),
        Ok(_) => {
            coroutine.call_method0("close")?;
            return Err(PyRuntimeError::new_err("M0 handler unexpectedly suspended"));
        }
    };
    Ok(Payload {
        ok: result.get_item("ok")?.extract()?,
        count: result.get_item("count")?.extract()?,
    })
}

async fn python_handler(
    State(handler): State<Arc<Py<PyAny>>>,
) -> Result<axum::Json<Payload>, axum::http::StatusCode> {
    Python::attach(|py| call_python(py, &handler))
        .map(axum::Json)
        .map_err(|error| {
            eprintln!("Python handler failed: {error}");
            axum::http::StatusCode::INTERNAL_SERVER_ERROR
        })
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let mode = args.next().unwrap_or_else(|| "rust".to_owned());
    let address: SocketAddr = args
        .next()
        .unwrap_or_else(|| "127.0.0.1:8000".to_owned())
        .parse()?;
    if args.next().is_some() {
        return Err("usage: bessemer-m0 [rust|python] [host:port]".into());
    }
    let app = match mode.as_str() {
        "rust" => Router::new()
            .route("/", get(rust_handler))
            .route("/__m0__/mode", get(|| async { "rust" })),
        "python" => Router::new()
            .route("/", get(python_handler))
            .route("/__m0__/mode", get(|| async { "python" }))
            .with_state(Arc::new(python_function()?)),
        _ => return Err(format!("unknown mode {mode:?}; expected rust or python").into()),
    };
    let listener = TcpListener::bind(address).await?;
    println!("mode={mode} address={address}");
    axum::serve(listener, app).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::exceptions::PyValueError;

    #[tokio::test]
    async fn python_and_rust_handlers_return_the_same_payload() {
        let handler = Arc::new(python_function().unwrap());
        let python = python_handler(State(handler)).await.unwrap().0;
        assert_eq!(python, rust_handler().await.0);
    }

    #[test]
    fn suspended_handler_is_rejected() {
        Python::attach(|py| {
            let module = PyModule::from_code(
                py,
                c"class Wait:\n    def __await__(self):\n        yield None\nasync def handler():\n    await Wait()\n",
                c"suspended_handler.py",
                c"suspended_handler",
            )
            .unwrap();
            let handler = module.getattr("handler").unwrap().unbind();
            let error = call_python(py, &handler).unwrap_err();
            assert!(error.is_instance_of::<PyRuntimeError>(py));
        });
    }

    #[test]
    fn handler_exception_is_propagated() {
        Python::attach(|py| {
            let module = PyModule::from_code(
                py,
                c"async def handler():\n    raise ValueError('failure')\n",
                // Each test needs its own module name: tests run in parallel
                // and from_code returns the sys.modules entry for the name.
                c"failing_handler.py",
                c"failing_handler",
            )
            .unwrap();
            let handler = module.getattr("handler").unwrap().unbind();
            let error = call_python(py, &handler).unwrap_err();
            assert!(error.is_instance_of::<PyValueError>(py));
        });
    }
}
