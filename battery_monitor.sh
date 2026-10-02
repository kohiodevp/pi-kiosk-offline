#!/usr/bin/env bash
set -euo pipefail
umask 077

PROG="${0##*/}"
SYSFS_POWER_SUPPLY="${SYSFS_POWER_SUPPLY-/sys/class/power_supply}"
VCGENCMD="${VCGENCMD-vcgencmd}"
WARN_PCT="${WARN_PCT-30}"
CRIT_PCT="${CRIT_PCT-15}"
INTERVAL="${INTERVAL-300}"
HOOK="${HOOK:-}"
DAEMON=0
QUIET=0

usage() {
  printf '%s\n' \
    "Usage: ${PROG} [-h] [-w PCT] [-c PCT] [-d] [-i S]" \
    "              [--hook COMMANDE] [-q]" \
    "" \
    "Supervision de la batterie du kiosque (sysfs power_supply) :" \
    "capacite, etat, tension, autonomie estimee et flags de sous-" \
    "tension Raspberry Pi (vcgencmd)." \
    "" \
    "Options:" \
    "  -w, --warn PCT    seuil d'avertissement (defaut: ${WARN_PCT})" \
    "  -c, --crit PCT    seuil critique (defaut: ${CRIT_PCT})" \
    "  -d, --daemon      boucle periodique (defaut: passage unique)" \
    "  -i, --interval S  periode en mode boucle (defaut: ${INTERVAL})" \
    "  --hook COMMANDE   execute a l'entree en etat CRITIQUE" \
    "  -q, --quiet       n'affiche que le resume final" \
    "  -h, --help        affiche cette aide" \
    "" \
    "Variables d'environnement:" \
    "  SYSFS_POWER_SUPPLY  repertoire sysfs (defaut: ${SYSFS_POWER_SUPPLY})" \
    "  VCGENCMD            binaire vcgencmd (defaut: ${VCGENCMD})" \
    "" \
    "Codes de retour: 0 OK, 1 avertissement, 2 critique ou introuvable"
}

log() {
  local priority="$1"
  shift
  if [[ "${QUIET}" -eq 1 && "${priority}" != "err" ]]; then
    return 0
  fi
  printf '%s %s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "${priority^^}" "$PROG" "$*" >&2
  logger -p "user.${priority}" -t "$PROG" -- "$*" 2>/dev/null || true
}

die() {
  log err "$*"
  exit 2
}

check_config() {
  [[ "${WARN_PCT}" =~ ^[0-9]+$ ]] || die "seuil -w invalide: ${WARN_PCT}"
  [[ "${CRIT_PCT}" =~ ^[0-9]+$ ]] || die "seuil -c invalide: ${CRIT_PCT}"
  [[ "${WARN_PCT}" -gt "${CRIT_PCT}" ]] \
    || die "-w doit etre superieur a -c"
  [[ "${INTERVAL}" =~ ^[1-9][0-9]*$ ]] || die "-i invalide: ${INTERVAL}"
  [[ -d "${SYSFS_POWER_SUPPLY}" ]] \
    || die "sysfs introuvable: ${SYSFS_POWER_SUPPLY}"
}

read_file() {
  local path="$1"
  READ_VALUE=""
  [[ -r "${path}" ]] || return 1
  IFS= read -r READ_VALUE < "${path}" || true
  return 0
}

find_batteries() {
  local dir=""
  local kind=""
  BATTERIES=()
  for dir in "${SYSFS_POWER_SUPPLY}"/*; do
    [[ -d "${dir}" ]] || continue
    kind=""
    if read_file "${dir}/type"; then
      kind="${READ_VALUE}"
    fi
    if [[ "${kind}" == "Battery" ]]; then
      BATTERIES+=("${dir}")
      continue
    fi
    if [[ -z "${kind}" && -r "${dir}/capacity" && -r "${dir}/status" ]]; then
      BATTERIES+=("${dir}")
    fi
  done
  if [[ "${#BATTERIES[@]}" -eq 0 ]]; then
    return 1
  fi
  return 0
}

read_battery() {
  local dir="$1"
  BAT_NAME="${dir##*/}"
  BAT_CAPACITY="-1"
  BAT_STATUS="inconnu"
  BAT_VOLTAGE=""
  BAT_CURRENT=""
  BAT_CHARGE=""
  BAT_FULL=""
  BAT_DESIGN=""

  if read_file "${dir}/capacity"; then
    BAT_CAPACITY="${READ_VALUE}"
  fi
  if read_file "${dir}/status"; then
    BAT_STATUS="${READ_VALUE}"
  fi
  if read_file "${dir}/voltage_now"; then
    BAT_VOLTAGE="${READ_VALUE}"
  fi
  if read_file "${dir}/current_now"; then
    BAT_CURRENT="${READ_VALUE}"
  fi
  if read_file "${dir}/charge_now"; then
    BAT_CHARGE="${READ_VALUE}"
  fi
  if read_file "${dir}/charge_full"; then
    BAT_FULL="${READ_VALUE}"
  fi
  if read_file "${dir}/charge_full_design"; then
    BAT_DESIGN="${READ_VALUE}"
  fi
}

format_autonomy() {
  local hours=""
  AUTONOMY="n/a"
  if [[ "${BAT_STATUS}" != "Discharging" ]]; then
    AUTONOMY="n/a"
    return 0
  fi
  if [[ ! "${BAT_CHARGE}" =~ ^[0-9]+$ || ! "${BAT_CURRENT}" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  if [[ "${BAT_CURRENT}" -le 0 || "${BAT_CHARGE}" -le 0 ]]; then
    return 0
  fi
  hours="$(awk -v c="${BAT_CHARGE}" -v i="${BAT_CURRENT}" \
    'BEGIN { printf "%.2f", c / i }')"
  AUTONOMY="$(awk -v h="${hours}" \
    'BEGIN {
      total = int(h * 60)
      printf "%dh%02d", int(h), total % 60
    }')"
}

evaluate_capacity() {
  local pct="$1"
  local status="$2"
  if [[ ! "${pct}" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  case "${status}" in
    Charging | Full | "Not charging")
      return 0
      ;;
  esac
  if [[ "${pct}" -le "${CRIT_PCT}" ]]; then
    WORST=2
    return 0
  fi
  if [[ "${pct}" -le "${WARN_PCT}" && "${WORST}" -lt 1 ]]; then
    WORST=1
  fi
}

read_throttled() {
  local raw=""
  THROTTLED=""
  command -v "${VCGENCMD}" >/dev/null 2>&1 || return 0
  raw="$("${VCGENCMD}" get_throttled 2>/dev/null)" || return 0
  THROTTLED="${raw#*=}"
  [[ "${THROTTLED}" =~ ^0x[0-9a-fA-F]+$ ]] || THROTTLED=""
}

report_throttled() {
  local value=0
  local messages=()
  [[ -n "${THROTTLED}" ]] || return 0
  value=$(( THROTTLED ))
  if (( value & 1 )); then
    messages+=("sous-tension actuelle")
    WORST=2
  fi
  if (( value & 4 )); then
    messages+=("throttling actif")
    if [[ "${WORST}" -lt 2 ]]; then WORST=1; fi
  fi
  if (( value & 65536 )); then
    messages+=("episode de sous-tension passe")
    if [[ "${WORST}" -lt 1 ]]; then WORST=1; fi
  fi
  if (( value & 262144 )); then
    messages+=("episode de throttling passe")
    if [[ "${WORST}" -lt 1 ]]; then WORST=1; fi
  fi
  local msg=""
  for msg in "${messages[@]}"; do
    log warning "alimentation: ${msg} (${THROTTLED})"
  done
  THROTTLED_SUMMARY="${#messages[@]} anomalie(s) (${THROTTLED})"
}

run_hook() {
  local pct="$1"
  [[ -n "${HOOK}" ]] || return 0
  log info "declenchement du hook (etat critique, ${pct}%)"
  BATTERY_PERCENT="${pct}" BATTERY_NAME="${BAT_NAME}" \
    sh -c "${HOOK}" || log warning "hook en echec"
}

report_batteries() {
  local dir=""
  local pct=""
  local voltage=""
  local health=""
  for dir in "${BATTERIES[@]}"; do
    read_battery "${dir}"
    format_autonomy
    pct="${BAT_CAPACITY}"
    voltage="n/a"
    health="n/a"
    if [[ "${BAT_VOLTAGE}" =~ ^[0-9]+$ ]]; then
      voltage="$(awk -v v="${BAT_VOLTAGE}" \
        'BEGIN { printf "%.2fV", v / 1000000 }')"
    fi
    if [[ "${BAT_FULL}" =~ ^[0-9]+$ \
      && "${BAT_DESIGN}" =~ ^[0-9]+$ && "${BAT_DESIGN}" -gt 0 ]]; then
      health="$(awk -v f="${BAT_FULL}" -v d="${BAT_DESIGN}" \
        'BEGIN { printf "%d%%", (100 * f) / d }')"
    fi
    printf 'Batterie %-14s %4s%% %-12s %6s %7s %s\n' \
      "${BAT_NAME}" "${pct}" "${BAT_STATUS}" "${voltage}" \
      "${AUTONOMY}" "${health}"
    evaluate_capacity "${pct}" "${BAT_STATUS}"
    if [[ "${WORST}" -eq 2 && "${LAST_WORST}" -ne 2 && "${DAEMON}" -eq 1 ]]; then
      run_hook "${pct}"
    fi
    if [[ "${WORST}" -eq 2 && "${DAEMON}" -eq 0 ]]; then
      run_hook "${pct}"
    fi
    LAST_WORST="${WORST}"
  done
}

summary() {
  local label="OK"
  case "${WORST}" in
    1) label="AVERTISSEMENT" ;;
    2) label="CRITIQUE" ;;
  esac
  printf 'Etat: %s (seuils %s%%/%s%%)' "${label}" "${WARN_PCT}" "${CRIT_PCT}"
  if [[ -n "${THROTTLED_SUMMARY}" ]]; then
    printf ' | vcgencmd: %s' "${THROTTLED_SUMMARY}"
  fi
  printf '\n'
}

run_once() {
  WORST=0
  THROTTLED_SUMMARY=""
  if ! find_batteries; then
    log err "aucune batterie dans ${SYSFS_POWER_SUPPLY}"
    printf 'Etat: CRITIQUE (aucune batterie detectee)\n'
    return 2
  fi
  report_batteries
  read_throttled
  report_throttled
  summary
  return "${WORST}"
}

main() {
  local opt=""
  while [[ $# -gt 0 ]]; do
    opt="$1"
    case "${opt}" in
      -w | --warn)
        [[ $# -ge 2 ]] || { usage >&2; return 64; }
        WARN_PCT="$2"
        shift 2
        ;;
      -c | --crit)
        [[ $# -ge 2 ]] || { usage >&2; return 64; }
        CRIT_PCT="$2"
        shift 2
        ;;
      -d | --daemon)
        DAEMON=1
        shift
        ;;
      -i | --interval)
        [[ $# -ge 2 ]] || { usage >&2; return 64; }
        INTERVAL="$2"
        shift 2
        ;;
      --hook)
        [[ $# -ge 2 ]] || { usage >&2; return 64; }
        HOOK="$2"
        shift 2
        ;;
      -q | --quiet)
        QUIET=1
        shift
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        printf 'Option inconnue: %s\n' "${opt}" >&2
        usage >&2
        return 64
        ;;
    esac
  done

  check_config
  if [[ "${DAEMON}" -eq 0 ]]; then
    run_once
    return $?
  fi

  log info "mode boucle, periode ${INTERVAL}s"
  while true; do
    run_once || true
    sleep "${INTERVAL}"
  done
}

BATTERIES=()
READ_VALUE=""
WORST=0
LAST_WORST=0
THROTTLED_SUMMARY=""
main "$@"
