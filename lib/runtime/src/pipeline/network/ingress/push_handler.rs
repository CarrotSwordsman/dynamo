// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use super::*;

use crate::engine::{AsyncEngineContext, ResponseStream};
use crate::metrics::prometheus_names::work_handler;
use crate::metrics::work_handler_perf::{
    WORK_HANDLER_NETWORK_TRANSIT_SECONDS, WORK_HANDLER_TIME_TO_FIRST_RESPONSE_SECONDS,
};
use crate::pipeline::ManyIn;
use crate::pipeline::context::Controller;
use crate::protocols::maybe_error::MaybeError;
use prometheus::{Histogram, IntCounter, IntCounterVec, IntGauge};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Instant;
use tracing::Instrument;
use tracing::info_span;

/// Metrics configuration for profiling work handlers
#[derive(Clone, Debug)]
pub struct WorkHandlerMetrics {
    pub request_counter: IntCounter,
    pub request_duration: Histogram,
    pub inflight_requests: IntGauge,
    pub request_bytes: IntCounter,
    pub response_bytes: IntCounter,
    pub error_counter: IntCounterVec,
    pub cancellation_total: IntCounter,
}

impl WorkHandlerMetrics {
    pub fn new(
        request_counter: IntCounter,
        request_duration: Histogram,
        inflight_requests: IntGauge,
        request_bytes: IntCounter,
        response_bytes: IntCounter,
        error_counter: IntCounterVec,
        cancellation_total: IntCounter,
    ) -> Self {
        Self {
            request_counter,
            request_duration,
            inflight_requests,
            request_bytes,
            response_bytes,
            error_counter,
            cancellation_total,
        }
    }

    /// Create WorkHandlerMetrics from an endpoint using its built-in labeling
    pub fn from_endpoint(
        endpoint: &crate::component::Endpoint,
        metrics_labels: Option<&[(&str, &str)]>,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let metrics_labels = metrics_labels.unwrap_or(&[]);
        let metrics = endpoint.metrics();
        let request_counter = metrics.create_intcounter(
            work_handler::REQUESTS_TOTAL,
            "Total number of requests processed by work handler",
            metrics_labels,
        )?;

        // Custom buckets for inference workloads: retain sub-second resolution for
        // fast operations, extend well beyond the default 10s ceiling to capture
        // long-running generation requests that can last minutes.
        let request_duration_buckets = vec![
            0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0,
            300.0, 600.0,
        ];
        let request_duration = metrics.create_histogram(
            work_handler::REQUEST_DURATION_SECONDS,
            "Time spent processing requests by work handler",
            metrics_labels,
            Some(request_duration_buckets),
        )?;

        let inflight_requests = metrics.create_intgauge(
            work_handler::INFLIGHT_REQUESTS,
            "Number of requests currently being processed by work handler",
            metrics_labels,
        )?;

        let request_bytes = metrics.create_intcounter(
            work_handler::REQUEST_BYTES_TOTAL,
            "Total number of bytes received in requests by work handler",
            metrics_labels,
        )?;

        let response_bytes = metrics.create_intcounter(
            work_handler::RESPONSE_BYTES_TOTAL,
            "Total number of bytes sent in responses by work handler",
            metrics_labels,
        )?;

        let error_counter = metrics.create_intcountervec(
            work_handler::ERRORS_TOTAL,
            "Total number of errors in work handler processing",
            &[work_handler::ERROR_TYPE_LABEL],
            metrics_labels,
        )?;

        let cancellation_total = metrics.create_intcounter(
            work_handler::CANCELLATION_TOTAL,
            "Total number of requests cancelled by work handler",
            metrics_labels,
        )?;

        Ok(Self::new(
            request_counter,
            request_duration,
            inflight_requests,
            request_bytes,
            response_bytes,
            error_counter,
            cancellation_total,
        ))
    }
}

// RAII guard to ensure inflight gauge is decremented, request duration is observed,
// and lifecycle logs are emitted on all code paths.
struct RequestMetricsGuard {
    inflight_requests: prometheus::IntGauge,
    request_duration: prometheus::Histogram,
    start_time: Instant,
    request_id: Option<String>,
}

impl Drop for RequestMetricsGuard {
    fn drop(&mut self) {
        self.inflight_requests.dec();
        self.request_duration
            .observe(self.start_time.elapsed().as_secs_f64());
        if let Some(request_id) = &self.request_id {
            tracing::info!(request_id = %request_id, "request completed");
        }
    }
}

#[async_trait]
impl<T: Data, U: Data> PushWorkHandler for Ingress<SingleIn<T>, ManyOut<U>>
where
    T: Data + for<'de> Deserialize<'de> + std::fmt::Debug,
    U: Data + Serialize + MaybeError + std::fmt::Debug,
{
    fn add_metrics(
        &self,
        endpoint: &crate::component::Endpoint,
        metrics_labels: Option<&[(&str, &str)]>,
    ) -> Result<()> {
        // Call the Ingress-specific add_metrics implementation
        use crate::pipeline::network::Ingress;
        Ingress::add_metrics(self, endpoint, metrics_labels)
    }

    fn set_endpoint_health_check_notifier(&self, notifier: Arc<tokio::sync::Notify>) -> Result<()> {
        use crate::pipeline::network::Ingress;
        self.endpoint_health_check_notifier
            .set(notifier)
            .map_err(|_| anyhow::anyhow!("Endpoint health check notifier already set"))?;
        Ok(())
    }

    async fn handle_payload(
        &self,
        payload: Bytes,
        request_id: Option<String>,
    ) -> Result<(), PipelineError> {
        let t2_wallclock_ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        let start_time = std::time::Instant::now();

        // Increment inflight and ensure it's decremented on all exits via RAII guard
        let _inflight_guard = self.metrics().map(|m| {
            m.request_counter.inc();
            m.inflight_requests.inc();
            m.request_bytes.inc_by(payload.len() as u64);
            if let Some(rid) = &request_id {
                tracing::info!(request_id = %rid, "request received");
            }
            RequestMetricsGuard {
                inflight_requests: m.inflight_requests.clone(),
                request_duration: m.request_duration.clone(),
                start_time,
                request_id: request_id.clone(),
            }
        });

        // decode the control message and the request
        let msg = TwoPartCodec::default()
            .decode_message(payload)?
            .into_message_type();

        // we must have a header and a body
        // it will be held by this closure as a Some(permit)
        let (control_msg, request) = match msg {
            TwoPartMessageType::HeaderAndData(header, data) => {
                tracing::trace!(
                    "received two part message with ctrl: {} bytes, data: {} bytes",
                    header.len(),
                    data.len()
                );
                let control_msg: RequestControlMessage = match serde_json::from_slice(&header) {
                    Ok(cm) => cm,
                    Err(err) => {
                        let json_str = String::from_utf8_lossy(&header);
                        if let Some(m) = self.metrics() {
                            m.error_counter
                                .with_label_values(&[work_handler::error_types::DESERIALIZATION])
                                .inc();
                        }
                        return Err(PipelineError::DeserializationError(format!(
                            "Failed deserializing to RequestControlMessage. err={err}, json_str={json_str}, header_len={}",
                            header.len(),
                        )));
                    }
                };
                let request: T = serde_json::from_slice(&data)?;
                (control_msg, request)
            }
            _ => {
                if let Some(m) = self.metrics() {
                    m.error_counter
                        .with_label_values(&[work_handler::error_types::INVALID_MESSAGE])
                        .inc();
                }
                return Err(PipelineError::Generic(String::from(
                    "Unexpected message from work queue; unable extract a TwoPartMessage with a header and data",
                )));
            }
        };

        // Compute network transit time (T2 - T1) using cross-process wall-clock timestamps
        if let Some(t1_ns) = control_msg.frontend_send_ts_ns {
            let transit_ns = t2_wallclock_ns.saturating_sub(t1_ns);
            WORK_HANDLER_NETWORK_TRANSIT_SECONDS.observe(transit_ns as f64 / 1_000_000_000.0);
        }

        // extend request with context
        tracing::trace!(
            request_id = %control_msg.id,
            metadata_entries = control_msg.metadata.len(),
            "received control message"
        );
        tracing::trace!("received request: {:?}", request);
        let request: context::Context<T> =
            Context::with_id_and_metadata(request, control_msg.id, control_msg.metadata);

        // todo - eventually have a handler class which will returned an abstracted object, but for now,
        // we only support tcp here, so we can just unwrap the connection info
        tracing::trace!("creating tcp response stream");
        let mut publisher = tcp::client::TcpClient::create_response_stream(
            request.context(),
            control_msg.connection_info,
            self.metrics().map(|m| m.cancellation_total.clone()),
        )
        .await
        .map_err(|e| {
            if let Some(m) = self.metrics() {
                m.error_counter
                    .with_label_values(&[work_handler::error_types::RESPONSE_STREAM])
                    .inc();
            }
            PipelineError::Generic(format!("Failed to create response stream: {:?}", e,))
        })?;

        tracing::trace!("calling generate");
        let stream = self
            .segment
            .get()
            .expect("segment not set")
            .generate(request)
            .await
            .map_err(|e| {
                if let Some(m) = self.metrics() {
                    m.error_counter
                        .with_label_values(&[work_handler::error_types::GENERATE])
                        .inc();
                }
                PipelineError::GenerateError(e)
            });

        // the prolouge is sent to the client to indicate that the stream is ready to receive data
        // or if the generate call failed, the error is sent to the client
        let stream = match stream {
            Ok(stream) => {
                tracing::trace!("Successfully generated response stream; sending prologue");
                let _result = publisher.send_prologue(None).await;
                WORK_HANDLER_TIME_TO_FIRST_RESPONSE_SECONDS
                    .observe(start_time.elapsed().as_secs_f64());
                stream
            }
            Err(e) => {
                let error_string = e.to_string();

                #[cfg(debug_assertions)]
                {
                    tracing::debug!(
                        "Failed to generate response stream (with debug backtrace): {:?}",
                        e
                    );
                }
                #[cfg(not(debug_assertions))]
                {
                    tracing::error!("Failed to generate response stream: {error_string}");
                }

                let _result = publisher.send_prologue(Some(error_string)).await;
                Err(e)?
            }
        };

        self.pump_response_stream(stream, &publisher).await;

        // Ensure the metrics guard is not dropped until the end of the function.
        // Drop fires "request completed" log via RAII.
        drop(_inflight_guard);

        Ok(())
    }
}

#[async_trait]
impl<T, U> PushWorkHandler for Ingress<ManyIn<T>, ManyOut<U>>
where
    T: Data + for<'de> Deserialize<'de> + std::fmt::Debug,
    U: Data + Serialize + MaybeError + std::fmt::Debug,
{
    fn add_metrics(
        &self,
        endpoint: &crate::component::Endpoint,
        metrics_labels: Option<&[(&str, &str)]>,
    ) -> Result<()> {
        use crate::pipeline::network::Ingress;
        Ingress::add_metrics(self, endpoint, metrics_labels)
    }

    fn set_endpoint_health_check_notifier(&self, notifier: Arc<tokio::sync::Notify>) -> Result<()> {
        self.endpoint_health_check_notifier
            .set(notifier)
            .map_err(|_| anyhow::anyhow!("Endpoint health check notifier already set"))?;
        Ok(())
    }

    async fn handle_payload(
        &self,
        payload: Bytes,
        request_id: Option<String>,
    ) -> Result<(), PipelineError> {
        let t2_wallclock_ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        let start_time = std::time::Instant::now();

        let _inflight_guard = self.metrics().map(|m| {
            m.request_counter.inc();
            m.inflight_requests.inc();
            m.request_bytes.inc_by(payload.len() as u64);
            if let Some(rid) = &request_id {
                tracing::info!(request_id = %rid, "bidirectional request received");
            }
            RequestMetricsGuard {
                inflight_requests: m.inflight_requests.clone(),
                request_duration: m.request_duration.clone(),
                start_time,
                request_id: request_id.clone(),
            }
        });

        // Bidirectional envelopes are header-only — all request frames
        // (including the first) flow on the request-stream socket once
        // it's dialed in. A HeaderAndData payload here would mean the
        // sender used the unary wire shape; reject it.
        let msg = TwoPartCodec::default()
            .decode_message(payload)?
            .into_message_type();

        let control_msg = match msg {
            TwoPartMessageType::HeaderOnly(header) => serde_json::from_slice::<RequestControlMessage>(&header).map_err(|err| {
                let json_str = String::from_utf8_lossy(&header);
                if let Some(m) = self.metrics() {
                    m.error_counter
                        .with_label_values(&[work_handler::error_types::DESERIALIZATION])
                        .inc();
                }
                PipelineError::DeserializationError(format!(
                    "Failed deserializing to RequestControlMessage. err={err}, json_str={json_str}"
                ))
            })?,
            _ => {
                if let Some(m) = self.metrics() {
                    m.error_counter
                        .with_label_values(&[work_handler::error_types::INVALID_MESSAGE])
                        .inc();
                }
                return Err(PipelineError::Generic(String::from(
                    "bidirectional engine received a non-header-only envelope",
                )));
            }
        };

        if !matches!(control_msg.request_type, RequestType::ManyIn) {
            if let Some(m) = self.metrics() {
                m.error_counter
                    .with_label_values(&[work_handler::error_types::INVALID_MESSAGE])
                    .inc();
            }
            return Err(PipelineError::Generic(String::from(
                "bidirectional engine received a non-ManyIn request envelope",
            )));
        }

        let req_stream_conn_info = control_msg
            .request_stream_connection_info
            .clone()
            .ok_or_else(|| {
                PipelineError::Generic(String::from(
                    "bidirectional control message missing request_stream_connection_info",
                ))
            })?;

        if let Some(t1_ns) = control_msg.frontend_send_ts_ns {
            let transit_ns = t2_wallclock_ns.saturating_sub(t1_ns);
            WORK_HANDLER_NETWORK_TRANSIT_SECONDS.observe(transit_ns as f64 / 1_000_000_000.0);
        }

        // Build the shared engine context — same id on both halves.
        let context_arc: Arc<dyn AsyncEngineContext> =
            Arc::new(Controller::new(control_msg.id.clone()));

        // Open the response stream (worker → upstream).
        let mut publisher = tcp::client::TcpClient::create_response_stream(
            context_arc.clone(),
            control_msg.connection_info,
            self.metrics().map(|m| m.cancellation_total.clone()),
        )
        .await
        .map_err(|e| {
            if let Some(m) = self.metrics() {
                m.error_counter
                    .with_label_values(&[work_handler::error_types::RESPONSE_STREAM])
                    .inc();
            }
            PipelineError::Generic(format!("Failed to create response stream: {:?}", e))
        })?;

        // Open the request stream (upstream → worker).
        let request_stream_recv = tcp::client::TcpClient::create_request_stream(
            context_arc.clone(),
            req_stream_conn_info,
            None,
        )
        .await
        .map_err(|e| {
            if let Some(m) = self.metrics() {
                m.error_counter
                    .with_label_values(&[work_handler::error_types::RESPONSE_STREAM])
                    .inc();
            }
            PipelineError::Generic(format!("Failed to create request stream: {:?}", e))
        })?;

        // Forwarder: deserialize raw bytes off the request socket into `T`
        // and feed the engine's `ManyIn<T>` input. Every request frame
        // (including the first) flows over this socket — the envelope is
        // header-only.
        let (frame_tx, frame_rx) = tokio::sync::mpsc::channel::<T>(8);
        let forwarder_ctx = context_arc.clone();
        tokio::spawn(async move {
            let mut rx = request_stream_recv.rx;
            while let Some(bytes) = rx.recv().await {
                if forwarder_ctx.is_killed() {
                    break;
                }
                match serde_json::from_slice::<T>(&bytes) {
                    Ok(item) => {
                        if frame_tx.send(item).await.is_err() {
                            tracing::debug!(
                                "engine consumer dropped; bidirectional input forwarder exiting"
                            );
                            break;
                        }
                    }
                    Err(e) => {
                        tracing::error!(
                            error = %e,
                            "failed to deserialize bidirectional request frame; killing context"
                        );
                        forwarder_ctx.kill();
                        break;
                    }
                }
            }
        });

        let input_stream: crate::engine::DataStream<T> =
            Box::pin(tokio_stream::wrappers::ReceiverStream::new(frame_rx));
        let request: ManyIn<T> = ResponseStream::new(input_stream, context_arc.clone());

        let stream = self
            .segment
            .get()
            .expect("segment not set")
            .generate(request)
            .await
            .map_err(|e| {
                if let Some(m) = self.metrics() {
                    m.error_counter
                        .with_label_values(&[work_handler::error_types::GENERATE])
                        .inc();
                }
                PipelineError::GenerateError(e)
            });

        let stream = match stream {
            Ok(stream) => {
                let _result = publisher.send_prologue(None).await;
                WORK_HANDLER_TIME_TO_FIRST_RESPONSE_SECONDS
                    .observe(start_time.elapsed().as_secs_f64());
                stream
            }
            Err(e) => {
                let error_string = e.to_string();
                #[cfg(debug_assertions)]
                {
                    tracing::debug!(
                        "bidirectional generate() failed (with debug backtrace): {:?}",
                        e
                    );
                }
                #[cfg(not(debug_assertions))]
                {
                    tracing::error!("bidirectional generate() failed: {error_string}");
                }
                let _result = publisher.send_prologue(Some(error_string)).await;
                Err(e)?
            }
        };

        self.pump_response_stream(stream, &publisher).await;

        drop(_inflight_guard);

        Ok(())
    }
}
