param(
    [switch]$Apply
)

if ($Apply) {
    py run_pipeline.py repair-image-storage-index --apply
} else {
    py run_pipeline.py repair-image-storage-index
}
