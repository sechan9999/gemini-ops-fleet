# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The event bus that drives the fleet.
#
# Business changes write an Activity row and publish here; the push
# subscription delivers to the Cloud Run endpoint, which appends the event to
# the stream and drains it. The service stays private -- delivery is
# authenticated with an OIDC token minted for a dedicated push identity, so
# opening the service to the public internet is never required to make the
# asynchronous path work.

resource "google_pubsub_topic" "fleet_events" {
  name    = "fleet-events"
  project = var.project_id
}

# A dedicated identity for delivery, rather than reusing the runtime service
# account. Push should be able to invoke the service and nothing else.
resource "google_service_account" "pubsub_push" {
  account_id   = "fleet-pubsub-push"
  display_name = "Pub/Sub push to the fleet"
  project      = var.project_id
}

resource "google_cloud_run_service_iam_member" "pubsub_can_invoke" {
  project  = var.project_id
  location = var.region
  service  = var.project_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_push.email}"
}

# Pub/Sub's own service agent needs permission to mint tokens as the push
# identity above. Without this the subscription is created successfully and
# then every delivery fails with 401 -- a quiet failure worth naming.
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_service_account_iam_member" "pubsub_mints_tokens" {
  service_account_id = google_service_account.pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "fleet_events_push" {
  name    = "fleet-events-push"
  topic   = google_pubsub_topic.fleet_events.id
  project = var.project_id

  # The drain claims each event in its own transaction, so a redelivery costs
  # one duplicate attempt rather than a duplicated side effect.
  ack_deadline_seconds       = 60
  message_retention_duration = "3600s"

  push_config {
    push_endpoint = "${var.service_url}/fleet/trigger/pubsub"

    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
    }
  }

  depends_on = [google_service_account_iam_member.pubsub_mints_tokens]
}

variable "service_url" {
  type        = string
  description = "Base URL of the deployed Cloud Run service."
}

output "fleet_events_topic" {
  value       = google_pubsub_topic.fleet_events.name
  description = "Publish fleet events here to drive the agents."
}
