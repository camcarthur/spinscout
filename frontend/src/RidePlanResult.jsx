import { useEffect, useMemo, useState } from 'react'
import RidePlanPreviewMap from './RidePlanPreviewMap'

/**
 * Build a single, unified candidate list from the ride plan response.
 * The API still returns a primary route + an `alternatives` array; in the UI
 * we treat them as a single, indexable list so the active card can swap any
 * of them onto the map without re-fetching.
 */
function buildCandidates(plan) {
  if (!plan) return []

  const primary = {
    name: plan.route_name || 'Recommended route',
    score: plan.score,
    reason: plan.summary,
    summary: null,
    destination_label: plan.destination_label,
    destination_category: plan.destination_category,
    destination_distance_miles: plan.destination_distance_miles,
    distance_miles: plan.route_distance_miles,
    elevation_ft: plan.route_elevation_ft,
    duration_min: plan.route_duration_min,
    route_polyline: plan.route_polyline,
    route_start_lat: plan.route_start_lat,
    route_start_lng: plan.route_start_lng,
    trail_percent: plan.trail_percent,
    bike_network_percent: plan.bike_network_percent,
    unpaved_percent: plan.unpaved_percent,
    major_road_percent: plan.major_road_percent,
    novelty_score: plan.novelty_score,
    overlap_percent: plan.overlap_percent,
    popularity_score: plan.popularity_score,
    surface_summary: plan.surface_summary,
    provider: plan.provider,
    isPrimary: true
  }

  const alternatives = (plan.alternatives || []).map((alt) => ({
    ...alt,
    isPrimary: false
  }))

  return [primary, ...alternatives]
}

function fmtMi(value) {
  return value != null ? `${Number(value).toFixed(1)} mi` : '— mi'
}

function fmtFt(value) {
  return value != null ? `${Math.round(Number(value))} ft` : '— ft'
}

function fmtMin(value) {
  return value != null ? `${Math.round(Number(value))} min` : '— min'
}

function fmtPct(value, suffix = '%') {
  return value != null ? `${Math.round(Number(value))}${suffix}` : '—'
}

function formatDurationSeconds(totalSeconds) {
  if (totalSeconds == null) return '—'
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function formatDateLabel(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString()
}

export default function RidePlanResult({
  plan,
  selectedStartLocation,
  exportTargets,
  onExport,
  exportingTargetId
}) {
  const candidates = useMemo(() => buildCandidates(plan), [plan])
  const [activeIndex, setActiveIndex] = useState(0)

  // Reset to the recommended route any time a fresh plan arrives.
  useEffect(() => {
    setActiveIndex(0)
  }, [plan])

  const activeCandidate = candidates[activeIndex] || candidates[0]
  if (!plan || !activeCandidate) return null

  const exportPayload = activeCandidate
    ? {
        route_name: activeCandidate.name || plan.title || 'Spin Scout Route',
        route_polyline: activeCandidate.route_polyline,
        // Used by the parent's exporter — keeps API-shape identical.
        ...activeCandidate
      }
    : null

  return (
    <div className="plan-stack">
      <div className="item plan-head">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <strong>{plan.title}</strong>
            <div className="meta" style={{ marginTop: '0.35rem' }}>{plan.summary}</div>
          </div>
          <span className="badge">{candidates.length} routes generated</span>
        </div>
        <div className="row" style={{ marginTop: '0.75rem' }}>
          {plan.location_label ? <span className="badge">{plan.location_label}</span> : null}
          {activeCandidate.destination_label ? (
            <span className="badge">Stop: {activeCandidate.destination_label}</span>
          ) : null}
          {plan.provider ? <span className="badge">{plan.provider}</span> : null}
          {plan.desired_style ? <span className="badge">{plan.desired_style}</span> : null}
          {plan.intensity_label ? <span className="badge">{plan.intensity_label}</span> : null}
        </div>
      </div>

      <RidePlanPreviewMap
        candidates={candidates}
        activeIndex={activeIndex}
        selectedStartLocation={selectedStartLocation}
        onSelectCandidate={setActiveIndex}
      />

      <div className="route-picker" role="tablist" aria-label="Generated route options">
        {candidates.map((candidate, index) => {
          const isActive = index === activeIndex
          return (
            <button
              key={`${candidate.name}-${index}`}
              type="button"
              role="tab"
              aria-selected={isActive}
              className={`route-card${isActive ? ' route-card--active' : ''}`}
              onClick={() => setActiveIndex(index)}
            >
              <div className="route-card__header">
                <strong>{candidate.name}</strong>
                {candidate.isPrimary ? <span className="badge badge--accent">Recommended</span> : null}
              </div>
              <div className="route-card__metrics">
                <span>{fmtMi(candidate.distance_miles)}</span>
                <span>·</span>
                <span>{fmtFt(candidate.elevation_ft)}</span>
                <span>·</span>
                <span>{fmtMin(candidate.duration_min)}</span>
              </div>
              <div className="route-card__signals">
                {candidate.bike_network_percent != null && candidate.bike_network_percent >= 10 ? (
                  <span className="chip chip--bike">{fmtPct(candidate.bike_network_percent)} bike paths</span>
                ) : null}
                {candidate.unpaved_percent != null && candidate.unpaved_percent >= 5 ? (
                  <span className="chip chip--gravel">{fmtPct(candidate.unpaved_percent)} unpaved</span>
                ) : null}
                {candidate.trail_percent != null && candidate.trail_percent >= 8 ? (
                  <span className="chip chip--trail">{fmtPct(candidate.trail_percent)} trail</span>
                ) : null}
                {candidate.major_road_percent != null ? (
                  <span className={`chip${candidate.major_road_percent > 25 ? ' chip--warn' : ''}`}>
                    {fmtPct(candidate.major_road_percent)} major rd
                  </span>
                ) : null}
                {candidate.novelty_score != null ? (
                  <span className="chip">{fmtPct(candidate.novelty_score)} novel</span>
                ) : null}
                {candidate.popularity_score != null ? (
                  <span className="chip">{fmtPct(candidate.popularity_score, '/100')} popular</span>
                ) : null}
                {candidate.destination_distance_miles != null ? (
                  <span className="chip">{fmtMi(candidate.destination_distance_miles)} to stop</span>
                ) : null}
              </div>
              {candidate.reason ? (
                <div className="route-card__reason">{candidate.reason}</div>
              ) : null}
            </button>
          )
        })}
      </div>

      <div className="small-grid">
        <div className="item">
          <strong>{fmtMi(activeCandidate.distance_miles)}</strong>
          <div className="meta">Distance</div>
        </div>
        <div className="item">
          <strong>{fmtFt(activeCandidate.elevation_ft)}</strong>
          <div className="meta">Climbing</div>
        </div>
        <div className="item">
          <strong>{fmtMin(activeCandidate.duration_min)}</strong>
          <div className="meta">Duration</div>
        </div>
        <div className="item">
          <strong>{fmtMi(plan.target_distance_miles)}</strong>
          <div className="meta">Target distance</div>
        </div>
        <div className="item">
          <strong>{fmtFt(plan.target_elevation_ft)}</strong>
          <div className="meta">Target climbing</div>
        </div>
        <div className="item">
          <strong>{activeCandidate.surface_summary ?? '—'}</strong>
          <div className="meta">Surface mix</div>
        </div>
      </div>

      <div className="item">
        <strong>Why this plan</strong>
        <div className="plan-points">
          {(plan.explanation || []).map((point, index) => (
            <div key={`${point}-${index}`} className="meta">{point}</div>
          ))}
        </div>
      </div>

      {plan.included_segments?.length ? (
        <div className="item">
          <strong>Included Strava segments (recommended route)</strong>
          <div className="meta" style={{ marginTop: '0.35rem' }}>
            Segments matched to the recommended route. Times shown are your current Strava bests when available.
          </div>
          <div className="segment-list">
            {plan.included_segments.map((segment) => (
              <div key={segment.id} className="segment-card">
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <strong>{segment.name}</strong>
                  {segment.popularity_score != null ? (
                    <span className="badge">{Math.round(segment.popularity_score)}/100 popular</span>
                  ) : null}
                </div>
                <div className="meta">
                  {segment.distance_miles ?? '—'} mi · {segment.avg_grade ?? '—'}% avg grade · {segment.route_overlap_percent ?? '—'}% of segment overlaps this route
                </div>
                <div className="meta">
                  {segment.athlete_count ?? '—'} athletes · {segment.effort_count ?? '—'} efforts · {segment.star_count ?? '—'} stars
                </div>
                <div className="meta">
                  {segment.current_time_seconds != null
                    ? `${segment.current_time_source || 'Current Strava time'}: ${formatDurationSeconds(segment.current_time_seconds)}`
                    : 'No current Strava time on this segment yet.'}
                  {segment.athlete_effort_count > 0 ? ` · ${segment.athlete_effort_count} efforts logged by you` : ''}
                  {segment.current_time_date ? ` · ${formatDateLabel(segment.current_time_date)}` : ''}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="item">
        <strong>Send this route to another app</strong>
        <div className="meta" style={{ marginTop: '0.35rem' }}>
          Exports the active route ({activeCandidate.name}). Pick another card above to export a different option.
        </div>
        <div className="export-grid">
          {exportTargets.map((target) => (
            <button
              key={target.id}
              type="button"
              className="secondary export-option"
              onClick={() => onExport(target, exportPayload)}
              disabled={exportingTargetId === target.id || !exportPayload?.route_polyline}
            >
              <strong>{exportingTargetId === target.id ? 'Preparing...' : target.label}</strong>
              <div className="meta">{target.format.toUpperCase()} · {target.hint}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
