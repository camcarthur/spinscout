import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

const DEFAULT_CENTER = [39.8283, -98.5795]
const DEFAULT_ZOOM = 4

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

export default function RidePlanPreviewMap({ plan, selectedStartLocation }) {
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

    if (plan?.route_polyline) {
      const points = decodePolyline(plan.route_polyline)
      if (points.length) {
        L.polyline(points, {
          color: '#155eef',
          weight: 4,
          opacity: 0.8
        }).addTo(layer)
        bounds.push(...points)
      }
    }

    if (plan?.route_start_lat != null && plan?.route_start_lng != null) {
      const routeStart = [plan.route_start_lat, plan.route_start_lng]
      L.circleMarker(routeStart, {
        radius: 8,
        color: '#fc4c02',
        fillColor: '#fc4c02',
        fillOpacity: 0.9,
        weight: 2
      })
        .bindTooltip(plan.route_name ? `Route start · ${plan.route_name}` : 'Route start')
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

      if (plan?.route_start_lat != null && plan?.route_start_lng != null) {
        L.polyline([selectedPoint, [plan.route_start_lat, plan.route_start_lng]], {
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
  }, [plan, selectedStartLocation])

  return <div ref={containerRef} className="map-canvas" />
}
