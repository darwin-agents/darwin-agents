output "ingress_class_name" {
  value       = var.ingress_class_name
  description = "IngressClass the platform Ingress binds to (empty-string semantics preserved when exposure=none)."
}

output "enabled" {
  value       = var.exposure != "none"
  description = "Whether an ingress controller + Ingress were installed."
}
