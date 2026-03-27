import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

const DEFAULT_CENTER = [39.8283, -98.5795]
const DEFAULT_ZOOM = 4

function formatCoordinate(value) {
  return Number(value).toFixed(4)
}

function formatActivityLabel(activity) {
  const location = [activity.location_city, activity.location_state].filter(Boolean).join(', ')
  return location ? `${activity.name} · ${location}` : activity.name
}

export default function LocationPickerMap({ activities, selectedLocation, onSelectLocation }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const activityLayerRef = useRef(null)
  const selectedLayerRef = useRef(null)
  const onSelectLocationRef = useRef(onSelectLocation)
  const shouldFitBoundsRef = useRef(true)
  const lastSelectionKeyRef = useRef('')

  useEffect(() => {
    onSelectLocationRef.current = onSelectLocation
  }, [onSelectLocation])

  useEffect(() => {
    shouldFitBoundsRef.current = true
  }, [activities])

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

    map.on('click', (event) => {
      const { lat, lng } = event.latlng
      onSelectLocationRef.current({
        lat,
        lng,
        label: `Custom point · ${formatCoordinate(lat)}, ${formatCoordinate(lng)}`,
        source: 'custom'
      })
    })

    activityLayerRef.current = L.layerGroup().addTo(map)
    selectedLayerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map

    window.setTimeout(() => map.invalidateSize(), 0)

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    const activityLayer = activityLayerRef.current
    const selectedLayer = selectedLayerRef.current
    if (!map || !activityLayer || !selectedLayer) return

    activityLayer.clearLayers()
    selectedLayer.clearLayers()

    const bounds = []
    for (const activity of activities) {
      if (activity.start_lat == null || activity.start_lng == null) continue

      const latLng = [activity.start_lat, activity.start_lng]
      bounds.push(latLng)

      const isSelected = selectedLocation?.source === 'activity' && selectedLocation?.activityId === activity.id
      const marker = L.circleMarker(latLng, {
        radius: isSelected ? 8 : 6,
        color: isSelected ? '#101828' : '#fc4c02',
        weight: 2,
        fillColor: isSelected ? '#101828' : '#fc4c02',
        fillOpacity: isSelected ? 0.9 : 0.65,
        bubblingMouseEvents: false
      })

      marker.bindTooltip(formatActivityLabel(activity))
      marker.on('click', () => {
        onSelectLocationRef.current({
          lat: activity.start_lat,
          lng: activity.start_lng,
          label: formatActivityLabel(activity),
          source: 'activity',
          activityId: activity.id
        })
      })
      marker.addTo(activityLayer)
    }

    const selectionKey = selectedLocation
      ? `${selectedLocation.lat}:${selectedLocation.lng}:${selectedLocation.activityId || selectedLocation.source}`
      : ''

    if (selectedLocation?.lat != null && selectedLocation?.lng != null) {
      const selectedLatLng = [selectedLocation.lat, selectedLocation.lng]
      const selectedMarker = L.circleMarker(selectedLatLng, {
        radius: 12,
        color: '#1d2939',
        weight: 3,
        fillColor: '#12b76a',
        fillOpacity: 0.42,
        bubblingMouseEvents: false
      })

      selectedMarker.bindPopup(`Selected start: ${selectedLocation.label}`)
      selectedMarker.addTo(selectedLayer)

      if (selectionKey !== lastSelectionKeyRef.current) {
        map.setView(selectedLatLng, Math.max(map.getZoom(), 11), { animate: false })
        lastSelectionKeyRef.current = selectionKey
      }
    } else {
      lastSelectionKeyRef.current = ''
    }

    if (!selectedLocation && bounds.length && shouldFitBoundsRef.current) {
      map.fitBounds(bounds, { padding: [24, 24] })
      shouldFitBoundsRef.current = false
    } else if (!selectedLocation && !bounds.length) {
      map.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: false })
    }

    map.invalidateSize()
  }, [activities, selectedLocation])

  return <div ref={containerRef} className="map-canvas" />
}
