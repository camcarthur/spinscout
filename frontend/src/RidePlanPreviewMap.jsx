import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

const DEFAULT_CENTER = [39.8283, -98.5795]
const DEFAULT_ZOOM = 4

const ACTIVE_COLOR = '#155eef'
const INACTIVE_COLOR = '#98a2b3'

function decodePolyline(encoded, precision = 5) {
  if (!encoded) return []

  const coordinates = []
  let index = 0
  let lat = 0
  let lng = 0
  const factor = 10 ** precision

  while (index < encoded.length) {
    let shift = 0
    let result = 0
    let byte = null

    do {
      byte = encoded.charCodeAt(index++) - 63
      result |= (byte & 0x1f) << shift
      shift += 5
    } while (byte >= 0x20)

    const deltaLat = result & 1 ? ~(result >> 1) : result >> 1
    lat += deltaLat

    shift = 0
    result = 0

    do {
      byte = encoded.charCodeAt(index++) - 63
      result |= (byte & 0x1f) << shift
      shift += 5
    } while (byte >= 0x20)

    const deltaLng = result & 1 ? ~(result >> 1) : result >> 1
    lng += deltaLng

    coordinates.push([lat / factor, lng / factor])
  }

  return coordinates
}

/**
 * Render every generated candidate on the map. The active one is solid + bold;
 * inactive candidates are dashed and clickable to swap which one is highlighted.
 */
export default function RidePlanPreviewMap({
  candidates = [],
  activeIndex = 0,
  selectedStartLocation,
  onSelectCandidate
}) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const layerRef = useRef(null)

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return

    const map = L.map(containerRef.current, {
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
      scrollWheelZoom: true
    })

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map)

    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    window.setTimeout(() => map.invalidateSize(), 0)

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return

    layer.clearLayers()
    const bounds = []

    // Inactive candidates first so the active one paints on top.
    candidates.forEach((candidate, index) => {
      if (index === activeIndex) return
      const points = decodePolyline(candidate.route_polyline)
      if (!points.length) return

      const polyline = L.polyline(points, {
        color: INACTIVE_COLOR,
        weight: 3,
        opacity: 0.55,
        dashArray: '6 6'
      }).addTo(layer)

      polyline.bindTooltip(candidate.name || `Route ${index + 1}`, { sticky: true })
      if (typeof onSelectCandidate === 'function') {
        polyline.on('click', () => onSelectCandidate(index))
      }
    })

    const active = candidates[activeIndex]
    if (active?.route_polyline) {
      const points = decodePolyline(active.route_polyline)
      if (points.length) {
        L.polyline(points, {
          color: ACTIVE_COLOR,
          weight: 5,
          opacity: 0.9
        }).addTo(layer)
        bounds.push(...points)
      }
    }

    if (active?.route_start_lat != null && active?.route_start_lng != null) {
      const routeStart = [active.route_start_lat, active.route_start_lng]
      L.circleMarker(routeStart, {
        radius: 8,
        color: '#fc4c02',
        fillColor: '#fc4c02',
        fillOpacity: 0.9,
        weight: 2
      })
        .bindTooltip(active.name ? `Route start · ${active.name}` : 'Route start')
        .addTo(layer)
      bounds.push(routeStart)
    }

    if (selectedStartLocation?.lat != null && selectedStartLocation?.lng != null) {
      const selectedPoint = [selectedStartLocation.lat, selectedStartLocation.lng]
      L.circleMarker(selectedPoint, {
        radius: 10,
        color: '#1d2939',
        fillColor: '#12b76a',
        fillOpacity: 0.45,
        weight: 3
      })
        .bindTooltip(`Chosen start · ${selectedStartLocation.label}`)
        .addTo(layer)
      bounds.push(selectedPoint)

      if (active?.route_start_lat != null && active?.route_start_lng != null) {
        L.polyline([selectedPoint, [active.route_start_lat, active.route_start_lng]], {
          color: '#98a2b3',
          weight: 2,
          dashArray: '6 6',
          opacity: 0.8
        }).addTo(layer)
      }
    }

    if (bounds.length) {
      map.fitBounds(bounds, { padding: [24, 24] })
    } else {
      map.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: false })
    }

    map.invalidateSize()
  }, [candidates, activeIndex, selectedStartLocation, onSelectCandidate])

  return <div ref={containerRef} className="map-canvas" />
}
