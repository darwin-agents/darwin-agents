output "storage_class" {
  value       = "hcloud-volumes"
  description = "StorageClass the CSI driver creates (use for PVCs)."
}

output "ready" {
  value       = true
  description = "Sentinel for depends_on ordering — true once CCM + CSI are installed."
  depends_on  = [helm_release.ccm, helm_release.csi]
}
