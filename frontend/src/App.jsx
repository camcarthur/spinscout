import { useEffect, useMemo, useState } from 'react'
import { fetchJson, getApiUrl } from './api'
import LocationPickerMap from './LocationPickerMap'
import RidePlanResult from './RidePlanResult'

function milesFromMeters(m) {
  if (m == null) return '—'
  return (m * 0.000621371).toFixed(1)
}

function feetFromMeters(m) {
  if (m == null) return '—'
  return Math.round(m * 3.28084)
}

function minutesFromSeconds(s) {
  if (s == null) return '—'
  return Math.round(s / 60)
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

function formatActivityLocation(activity) {
  const parts = [activity.location_city, activity.location_state, activity.location_country].filter(Boolean)
  return parts.join(', ')
}

function getConnectUrl() {
  const returnTo = `${window.location.origin}${window.location.pathname}`
  const params = new URLSearchParams({ return_to: returnTo })
  return `${getApiUrl()}/api/auth/strava/login?${params.toString()}`
}

const EXPORT_TARGETS = [
  {
    id: 'strava',
    label: 'Strava Routes',
    format: 'gpx',
    hint: 'Download a GPX track to import into Strava on the web.'
  },
  {
    id: 'garmin',
    label: 'Garmin Connect',
    format: 'tcx',
    hint: 'Download a TCX course file for Garmin Connect and Edge devices.'
  },
  {
    id: 'wahoo',
    label: 'Wahoo ELEMNT',
    format: 'gpx',
    hint: 'Download a GPX file to import into ELEMNT or another Wahoo route flow.'
  },
  {
    id: 'ridewithgps',
    label: 'Ride with GPS',
    format: 'gpx',
    hint: 'Download GPX for Ride with GPS route import.'
  },
  {
    id: 'komoot',
    label: 'Komoot',
    format: 'gpx',
    hint: 'Download GPX for Komoot route import.'
  },
  {
    id: 'hammerhead',
    label: 'Hammerhead',
    format: 'gpx',
    hint: 'Download GPX for Hammerhead Dashboard import.'
  },
  {
    id: 'other',
    label: 'Other GPS App',
    format: 'gpx',
    hint: 'Use a standards-based GPX file for most cycling apps and head units.'
  }
]

const PROMPT_SUGGESTIONS = [
  {
    label: 'Brewery loop',
    prompt: 'Casual ride to a brewery, around 22 miles, mellow climbing.'
  },
  {
    label: 'Gravel adventure',
    prompt: 'Gravel ride that prioritizes real unpaved paths, around 30 miles.'
  },
  {
    label: 'Coffee spin',
    prompt: 'Easy spin to a coffee shop somewhere new, around 18 miles.'
  },
  {
    label: 'Tempo on popular roads',
    prompt: 'Training ride on popular cyclist roads, around 35 miles, steady tempo.'
  }
]

function App() {
  const [status, setStatus] = useState('checking')
  const [routingStatus, setRoutingStatus] = useState({ enabled: false, provider: 'graphhopper' })
  const [users, setUsers] = useState([])
  const [selectedUserId, setSelectedUserId] = useState('')
  const [activities, setActivities] = useState([])
  const [feedback, setFeedback] = useState([])
  const [recommendations, setRecommendations] = useState([])
  const [busyAction, setBusyAction] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [selectedStartLocation, setSelectedStartLocation] = useState(null)
  const [ridePlan, setRidePlan] = useState(null)
  const [exportingTargetId, setExportingTargetId] = useState('')

  const [feedbackForm, setFeedbackForm] = useState({
    activity_id: '',
    perceived_effort: 5,
    enjoyment: 5,
    matched_intent: true,
    ride_style: '',
    notes: ''
  })

  const [recForm, setRecForm] = useState({
    ride_brief: '',
    desired_style: 'casual',
    min_distance_miles: '',
    max_distance_miles: '',
    min_elevation_ft: '',
    max_elevation_ft: '',
    target_effort: '',
    sport_type: '',
    location_radius_miles: '15'
  })

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const connected = params.get('connected')
    const userId = params.get('user_id')
    const authError = params.get('error')

    if (connected === '1' && userId) {
      setMessage('Strava connected successfully.')
      setSelectedUserId(userId)
      window.history.replaceState({}, '', window.location.pathname)
    } else if (connected === '0' && authError) {
      setError(`Strava authentication failed: ${authError.replace(/_/g, ' ')}`)
      window.history.replaceState({}, '', window.location.pathname)
    }

    loadInitial(userId)
  }, [])

  useEffect(() => {
    if (!selectedStartLocation || selectedStartLocation.source !== 'activity') return

    const stillExists = activities.some((activity) => activity.id === selectedStartLocation.activityId)
    if (!stillExists) {
      setSelectedStartLocation(null)
    }
  }, [activities, selectedStartLocation])

  async function loadInitial(preferredUserId) {
    try {
      const health = await fetchJson('/health')
      setStatus(health.status)
      setRoutingStatus({
        enabled: Boolean(health.routing_enabled),
        provider: health.routing_provider || 'graphhopper'
      })
      const userList = await fetchJson('/api/users')
      setUsers(userList)
      const picked = preferredUserId || userList[0]?.id?.toString() || ''
      if (picked) {
        setSelectedUserId(picked)
        await Promise.all([loadActivities(picked), loadFeedback(picked)])
      }
    } catch (err) {
      setError(err.message)
      setStatus('error')
    }
  }

  async function loadActivities(userId) {
    if (!userId) return
    const data = await fetchJson(`/api/users/${userId}/activities`)
    setActivities(data)
  }

  async function loadFeedback(userId) {
    if (!userId) return
    const data = await fetchJson(`/api/users/${userId}/feedback`)
    setFeedback(data)
  }

  const selectedUser = useMemo(
    () => users.find((u) => String(u.id) === String(selectedUserId)),
    [users, selectedUserId]
  )
  const mapActivities = useMemo(
    () => activities.filter((activity) => activity.start_lat != null && activity.start_lng != null),
    [activities]
  )
  const connectUrl = getConnectUrl()
  const isSyncing = busyAction === 'syncing'
  const isSavingFeedback = busyAction === 'feedback'
  const isGenerating = busyAction === 'planning'
  const hasBusyAction = Boolean(busyAction)
  const selectedStartSummary = selectedStartLocation
    ? selectedStartLocation.label
    : mapActivities.length
      ? 'Click a mapped start or any point on the map to anchor the route.'
      : 'Sync rides with GPS starts and they will appear here automatically.'
  const heroStats = [
    {
      label: 'Connected riders',
      value: users.length,
      detail: selectedUser ? `${selectedUser.firstname || selectedUser.username || 'Active'} selected` : 'Waiting for rider'
    },
    {
      label: 'Imported rides',
      value: activities.length,
      detail: activities.length ? 'Training history ready' : 'Sync recent activities'
    },
    {
      label: 'Mapped starts',
      value: mapActivities.length,
      detail: selectedStartLocation ? 'Start locked in' : 'Ready for a map pick'
    },
    {
      label: 'Feedback notes',
      value: feedback.length,
      detail: feedback.length ? 'Used to tune recommendations' : 'Optional, but helpful'
    }
  ]

  async function refreshUsers() {
    const userList = await fetchJson('/api/users')
    setUsers(userList)
  }

  async function syncActivities() {
    if (!selectedUserId) {
      setError('Connect Strava first.')
      return
    }
    setBusyAction('syncing')
    setError('')
    setMessage('')
    try {
      const result = await fetchJson(`/api/users/${selectedUserId}/sync?per_page=50`, {
        method: 'POST'
      })
      setMessage(`Synced ${result.synced} activities.`)
      setRidePlan(null)
      setRecommendations([])
      await loadActivities(selectedUserId)
      await refreshUsers()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyAction('')
    }
  }

  async function submitFeedback(e) {
    e.preventDefault()
    if (!selectedUserId) return

    setBusyAction('feedback')
    setError('')
    setMessage('')
    try {
      await fetchJson(`/api/users/${selectedUserId}/feedback`, {
        method: 'POST',
        body: JSON.stringify({
          ...feedbackForm,
          activity_id: Number(feedbackForm.activity_id),
          perceived_effort: Number(feedbackForm.perceived_effort),
          enjoyment: Number(feedbackForm.enjoyment)
        })
      })
      setMessage('Feedback saved.')
      setFeedbackForm((prev) => ({ ...prev, notes: '', ride_style: '' }))
      await loadFeedback(selectedUserId)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyAction('')
    }
  }

  async function getRecommendations(e) {
    e.preventDefault()
    if (!selectedUserId) return
    if (!recForm.ride_brief.trim()) {
      setError('Describe the ride you want first.')
      return
    }

    setBusyAction('planning')
    setError('')
    setMessage('')
    try {
      const payload = {
        ride_brief: recForm.ride_brief.trim(),
        desired_style: recForm.desired_style,
        min_distance_miles: recForm.min_distance_miles ? Number(recForm.min_distance_miles) : null,
        max_distance_miles: recForm.max_distance_miles ? Number(recForm.max_distance_miles) : null,
        min_elevation_ft: recForm.min_elevation_ft ? Number(recForm.min_elevation_ft) : null,
        max_elevation_ft: recForm.max_elevation_ft ? Number(recForm.max_elevation_ft) : null,
        target_effort: recForm.target_effort ? Number(recForm.target_effort) : null,
        sport_type: recForm.sport_type || null,
        start_lat: selectedStartLocation ? selectedStartLocation.lat : null,
        start_lng: selectedStartLocation ? selectedStartLocation.lng : null,
        start_label: selectedStartLocation ? selectedStartLocation.label : null,
        max_start_distance_miles:
          selectedStartLocation && recForm.location_radius_miles ? Number(recForm.location_radius_miles) : null
      }

      const result = await fetchJson(`/api/users/${selectedUserId}/ride-plan`, {
        method: 'POST',
        body: JSON.stringify(payload)
      })
      setRidePlan(result)
      setRecommendations(result.alternatives || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyAction('')
    }
  }

  async function exportRoute(target, payload) {
    // `payload` carries the *active* candidate the user picked in the result component.
    // Falls back to the recommended route on the plan for backwards compatibility.
    const polyline = payload?.route_polyline || ridePlan?.route_polyline
    const routeName = payload?.route_name || ridePlan?.route_name || ridePlan?.title || 'Spin Scout Route'
    if (!polyline) {
      setError('Generate a route first.')
      return
    }

    setExportingTargetId(target.id)
    setError('')
    setMessage('')
    try {
      const response = await fetch(`${getApiUrl()}/api/routes/export/${target.format}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          route_name: routeName,
          route_polyline: polyline,
          target: target.id
        })
      })

      if (!response.ok) {
        const contentType = response.headers.get('content-type') || ''
        if (contentType.includes('application/json')) {
          const payload = await response.json()
          throw new Error(payload?.detail || payload?.message || `Export failed: ${response.status}`)
        }
        throw new Error((await response.text()) || `Export failed: ${response.status}`)
      }

      const blob = await response.blob()
      const objectUrl = window.URL.createObjectURL(blob)
      const disposition = response.headers.get('content-disposition') || ''
      const filenameMatch = disposition.match(/filename="([^"]+)"/)
      const filename = filenameMatch?.[1] || `${routeName || 'spin-scout-route'}.${target.format}`

      const link = document.createElement('a')
      link.href = objectUrl
      link.download = filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(objectUrl)

      setMessage(`Downloaded ${target.label} ${target.format.toUpperCase()} file.`)
    } catch (err) {
      setError(err.message)
    } finally {
      setExportingTargetId('')
    }
  }

  return (
    <div className="page">
      <div className="hero hero-card">
        <div className="hero-copy">
          <div className="section-kicker">Adaptive route planner</div>
          <h1 className="brand">Spin Scout</h1>
          <p className="sub">
            Connect Strava, pick a start, describe the day you want, and get a route that feels tailored instead of generic, with real map data, destination logic, and cyclist popularity layered in.
          </p>
          <div className="row hero-status">
            <span className="badge">API: {status}</span>
            <span className="badge">
              Routing: {routingStatus.enabled ? `${routingStatus.provider} ready` : `${routingStatus.provider} setup needed`}
            </span>
            {selectedUser ? (
              <span className="badge">
                Active rider: {selectedUser.firstname || selectedUser.username || 'User'} {selectedUser.lastname || ''}
              </span>
            ) : (
              <span className="badge">No rider selected yet</span>
            )}
          </div>
          <div className="hero-actions">
            <a href={connectUrl}>
              <button className="primary">Connect Strava</button>
            </a>
            <button className="secondary" onClick={syncActivities} disabled={hasBusyAction || !selectedUserId}>
              {isSyncing ? 'Syncing rides...' : 'Sync Activities'}
            </button>
          </div>
        </div>

        <div className="hero-aside">
          <div className="hero-note">
            <div className="section-kicker">Session snapshot</div>
            <div className="stat-grid">
              {heroStats.map((stat) => (
                <div key={stat.label} className="stat-card">
                  <div className="stat-value">{stat.value}</div>
                  <div className="stat-label">{stat.label}</div>
                  <div className="meta">{stat.detail}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {message ? <div className="notice">{message}</div> : null}
      {error ? <div className="error">{error}</div> : null}

      <div className="grid" style={{ marginTop: '1rem' }}>
        <section className="card">
          <div className="section-kicker">Rider setup</div>
          <h2>Connected riders</h2>
          <div className="row">
            <select
              value={selectedUserId}
              onChange={async (e) => {
                const value = e.target.value
                setSelectedUserId(value)
                setSelectedStartLocation(null)
                setRidePlan(null)
                setRecommendations([])
                if (value) {
                  await Promise.all([loadActivities(value), loadFeedback(value)])
                } else {
                  setActivities([])
                  setFeedback([])
                }
              }}
            >
              <option value="">Select a rider</option>
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.firstname || user.username || 'User'} {user.lastname || ''} ({user.city || 'Unknown city'})
                </option>
              ))}
            </select>
          </div>
          {selectedUser ? (
            <div className="item tone-soft" style={{ marginTop: '0.75rem' }}>
              <strong>{selectedUser.firstname} {selectedUser.lastname}</strong>
              <div className="meta">@{selectedUser.username || 'no-username'}</div>
              <div className="meta">
                {selectedUser.city || 'Unknown city'}, {selectedUser.state || ''} {selectedUser.country || ''}
              </div>
            </div>
          ) : (
            <p className="meta">Connect Strava to create your first rider record.</p>
          )}
        </section>

        <section className="card">
          <div className="section-kicker">Planner</div>
          <h2>Planner request</h2>
          <form onSubmit={getRecommendations} className="stack">
            {!routingStatus.enabled ? (
              <div className="item tone-soft">
                <strong>Map routing setup needed</strong>
                <div className="meta" style={{ marginTop: '0.35rem' }}>
                  Add <code>GRAPHHOPPER_API_KEY</code> to the API environment and restart Docker to generate brand-new routes from map data.
                </div>
              </div>
            ) : null}

            <label>
              Describe the ride you want
              <textarea
                placeholder="Examples: popular with cyclists 40 mile ride, gravel ride to a brewery under 20 miles that prioritizes unpaved paths, training ride to a taco shop around 18 miles, or a scenic spin by a lake with steady climbing."
                value={recForm.ride_brief}
                onChange={(e) => setRecForm({ ...recForm, ride_brief: e.target.value })}
                required
              />
            </label>
            <div className="meta">
              The text box can now ask for destinations and scenery directly: brewery, taco shop, coffee stop, bakery, lake, river, park, gravel-focused terrain, and routes that stay on roads popular with cyclists.
            </div>
            <div className="prompt-shelf">
              {PROMPT_SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="prompt-chip"
                  onClick={() => setRecForm((prev) => ({ ...prev, ride_brief: suggestion.prompt }))}
                >
                  {suggestion.label}
                </button>
              ))}
            </div>

            <div className="map-picker">
              <div className="map-toolbar">
                <div>
                  <strong>Choose a ride start</strong>
                  <div className="meta">
                    Click one of your synced ride starts or click anywhere on the map for a custom start point. The planner will generate a new loop from there.
                  </div>
                </div>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    setSelectedStartLocation(null)
                    setRidePlan(null)
                    setRecommendations([])
                  }}
                  disabled={!selectedStartLocation}
                >
                  Clear start
                </button>
              </div>

              <LocationPickerMap
                activities={mapActivities}
                selectedLocation={selectedStartLocation}
                onSelectLocation={(location) => {
                  setSelectedStartLocation(location)
                  setRidePlan(null)
                  setRecommendations([])
                }}
              />

              <div className="map-status">
                <span className="badge">{mapActivities.length} mapped ride starts</span>
                {selectedStartLocation ? (
                  <span className="meta">Selected: {selectedStartLocation.label}</span>
                ) : (
                  <span className="meta">No start selected yet. You can still click the map before requesting a ride.</span>
                )}
              </div>

              <div className="selection-banner">
                <div>
                  <strong>{selectedStartLocation ? 'Start anchored' : 'Choose a starting point'}</strong>
                  <div className="meta">{selectedStartSummary}</div>
                </div>
                <span className="badge">{selectedStartLocation ? 'Start ready' : 'Waiting for start'}</span>
              </div>
            </div>

            <details className="refine-disclosure">
              <summary>
                <span className="refine-summary-label">Refine (optional)</span>
                <span className="meta">
                  Hard caps on distance, elevation, effort, sport type, or start radius. Leave blank to let the brief decide.
                </span>
              </summary>
              <div className="small-grid" style={{ marginTop: '0.85rem' }}>
                <label>
                  Desired style
                  <select
                    value={recForm.desired_style}
                    onChange={(e) => setRecForm({ ...recForm, desired_style: e.target.value })}
                  >
                    <option value="casual">Casual</option>
                    <option value="brewery">Brewery</option>
                    <option value="social">Social</option>
                    <option value="hard">Hard</option>
                    <option value="training">Training</option>
                    <option value="gravel">Gravel</option>
                    <option value="adventure">Adventure</option>
                  </select>
                </label>
                <label>
                  Sport type
                  <input
                    placeholder="Ride, GravelRide, MountainBikeRide..."
                    value={recForm.sport_type}
                    onChange={(e) => setRecForm({ ...recForm, sport_type: e.target.value })}
                  />
                </label>
                <label>
                  Min miles
                  <input
                    type="number"
                    value={recForm.min_distance_miles}
                    onChange={(e) => setRecForm({ ...recForm, min_distance_miles: e.target.value })}
                  />
                </label>
                <label>
                  Max miles
                  <input
                    type="number"
                    value={recForm.max_distance_miles}
                    onChange={(e) => setRecForm({ ...recForm, max_distance_miles: e.target.value })}
                  />
                </label>
                <label>
                  Min elevation (ft)
                  <input
                    type="number"
                    value={recForm.min_elevation_ft}
                    onChange={(e) => setRecForm({ ...recForm, min_elevation_ft: e.target.value })}
                  />
                </label>
                <label>
                  Max elevation (ft)
                  <input
                    type="number"
                    value={recForm.max_elevation_ft}
                    onChange={(e) => setRecForm({ ...recForm, max_elevation_ft: e.target.value })}
                  />
                </label>
                <label>
                  Target effort (1-10)
                  <input
                    type="number"
                    min="1"
                    max="10"
                    value={recForm.target_effort}
                    onChange={(e) => setRecForm({ ...recForm, target_effort: e.target.value })}
                  />
                </label>
                <label>
                  Start radius (mi)
                  <input
                    type="number"
                    min="1"
                    placeholder="15"
                    value={recForm.location_radius_miles}
                    onChange={(e) => setRecForm({ ...recForm, location_radius_miles: e.target.value })}
                  />
                </label>
              </div>
            </details>

            <div className="planner-submit">
              <button className="primary" disabled={!selectedUserId || hasBusyAction}>
                {isGenerating ? 'Generating routes...' : 'Generate ride options'}
              </button>
              <div className="meta">
                Spin Scout returns 3+ distinct route options. Click any of them on the map or in the result to set it as your pick.
              </div>
            </div>
          </form>
        </section>

        <section className="card">
          <div className="section-kicker">History</div>
          <h2>Activities</h2>
          <div className="meta" style={{ marginBottom: '0.75rem' }}>
            {activities.length} activities imported
          </div>
          <div className="list">
            {!activities.length ? (
              <div className="item tone-soft">
                <strong>No rides synced yet</strong>
                <div className="meta" style={{ marginTop: '0.35rem' }}>
                  Sync Strava activities to unlock map starts, personalization, and novelty checks.
                </div>
              </div>
            ) : null}
            {activities.map((activity) => (
              <div key={activity.id} className="item">
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <strong>{activity.name}</strong>
                  <span className="badge">{activity.sport_type || 'Unknown'}</span>
                </div>
                <div className="meta">
                  {milesFromMeters(activity.distance_m)} mi · {feetFromMeters(activity.total_elevation_gain_m)} ft · {minutesFromSeconds(activity.moving_time_s)} min
                </div>
                <div className="meta">
                  Avg HR: {activity.average_heartrate || '—'} · Avg W: {activity.average_watts || '—'} · Suffer: {activity.suffer_score || '—'}
                </div>
                {formatActivityLocation(activity) ? (
                  <div className="meta">Start: {formatActivityLocation(activity)}</div>
                ) : null}
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <div className="section-kicker">Learning loop</div>
          <h2>Ride feedback</h2>
          <form onSubmit={submitFeedback} className="stack">
            <label>
              Activity
              <select
                value={feedbackForm.activity_id}
                onChange={(e) => setFeedbackForm({ ...feedbackForm, activity_id: e.target.value })}
                required
              >
                <option value="">Choose an activity</option>
                {activities.map((activity) => (
                  <option key={activity.id} value={activity.id}>
                    {activity.name} — {milesFromMeters(activity.distance_m)} mi
                  </option>
                ))}
              </select>
            </label>

            <div className="small-grid">
              <label>
                Perceived effort (1-10)
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={feedbackForm.perceived_effort}
                  onChange={(e) => setFeedbackForm({ ...feedbackForm, perceived_effort: e.target.value })}
                />
              </label>
              <label>
                Enjoyment (1-10)
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={feedbackForm.enjoyment}
                  onChange={(e) => setFeedbackForm({ ...feedbackForm, enjoyment: e.target.value })}
                />
              </label>
            </div>

            <label>
              Ride style tag
              <input
                placeholder="casual, brewery, hard, social, gravel..."
                value={feedbackForm.ride_style}
                onChange={(e) => setFeedbackForm({ ...feedbackForm, ride_style: e.target.value })}
              />
            </label>

            <label className="row" style={{ alignItems: 'center' }}>
              <input
                style={{ width: 'auto' }}
                type="checkbox"
                checked={feedbackForm.matched_intent}
                onChange={(e) => setFeedbackForm({ ...feedbackForm, matched_intent: e.target.checked })}
              />
              Matched what I wanted that day
            </label>

            <label>
              Notes
              <textarea
                value={feedbackForm.notes}
                onChange={(e) => setFeedbackForm({ ...feedbackForm, notes: e.target.value })}
              />
            </label>

            <button className="primary" disabled={!selectedUserId || hasBusyAction}>
              {isSavingFeedback ? 'Saving feedback...' : 'Save feedback'}
            </button>
          </form>

          <h3 style={{ marginTop: '1rem' }}>Recent feedback</h3>
          <div className="list">
            {!feedback.length ? (
              <div className="item tone-soft">
                <strong>No feedback yet</strong>
                <div className="meta" style={{ marginTop: '0.35rem' }}>
                  Save a few notes on rides you loved or hated and the planner will get more personal.
                </div>
              </div>
            ) : null}
            {feedback.map((item) => (
              <div key={item.id} className="item">
                <div className="meta">
                  Activity #{item.activity_id} · effort {item.perceived_effort}/10 · enjoyment {item.enjoyment}/10
                </div>
                <div>{item.ride_style || 'No style tag'}</div>
                <div className="meta">{item.matched_intent ? 'Matched intent' : 'Did not match intent'}</div>
                {item.notes ? <div className="meta">{item.notes}</div> : null}
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="card" style={{ marginTop: '1rem' }}>
        <div className="section-kicker">Output</div>
        <h2>Generated ride options</h2>
        {ridePlan ? (
          <RidePlanResult
            plan={ridePlan}
            selectedStartLocation={selectedStartLocation}
            exportTargets={EXPORT_TARGETS}
            onExport={exportRoute}
            exportingTargetId={exportingTargetId}
          />
        ) : (
          <div className="empty-plan">
            <strong>Ready to sketch the day</strong>
            <div className="meta" style={{ marginTop: '0.45rem' }}>
              Pick a starting location, describe the kind of ride you want, and Spin Scout will generate a few distinct route options you can compare side-by-side on the map.
            </div>
            <div className="prompt-shelf" style={{ marginTop: '1rem' }}>
              {PROMPT_SUGGESTIONS.slice(0, 3).map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="prompt-chip"
                  onClick={() => setRecForm((prev) => ({ ...prev, ride_brief: suggestion.prompt }))}
                >
                  {suggestion.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

export default App
