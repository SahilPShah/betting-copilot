variable "IMAGE_TAG" {
  default = "0.1.0"
}

group "default" {
  targets = ["data-ingestion-job"]
}

target "data-ingestion-job" {
  context    = "."
  dockerfile = "Dockerfile.jobs"
  tags       = ["sshah523/betting-copilot-data-ingestion-job:${IMAGE_TAG}"]
  platforms  = ["linux/amd64"]
}
