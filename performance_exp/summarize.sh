#!/usr/bin/env bash
set -euo pipefail

file="${1:-results/all_metrics.csv}"

awk -F, '
{
  code[NR]=$2
  ttfb[NR]=$4
  total[NR]=$5
}
END {
  n=NR
  if (n==0) exit 1

  ok=0
  err=0
  for (i=1; i<=n; i++) {
    if (code[i] ~ /^2/) ok++
    else err++
  }

  # sort arrays
  for (i=1; i<=n; i++) {
    for (j=i+1; j<=n; j++) {
      if (ttfb[i] > ttfb[j]) { tmp=ttfb[i]; ttfb[i]=ttfb[j]; ttfb[j]=tmp }
      if (total[i] > total[j]) { tmp=total[i]; total[i]=total[j]; total[j]=tmp }
    }
  }

  def_p50 = int((n+1)*0.50)
  def_p95 = int((n+1)*0.95)
  if (def_p95 < 1) def_p95=1
  if (def_p95 > n) def_p95=n

  sum_ttfb=0
  sum_total=0
  for (i=1; i<=n; i++) {
    sum_ttfb += ttfb[i]
    sum_total += total[i]
  }

  printf("requests=%d ok=%d err=%d\n", n, ok, err)
  printf("ttfb_avg=%.3f ttfb_p50=%.3f ttfb_p95=%.3f\n", sum_ttfb/n, ttfb[def_p50], ttfb[def_p95])
  printf("total_avg=%.3f total_p50=%.3f total_p95=%.3f\n", sum_total/n, total[def_p50], total[def_p95])
}
' "$file"