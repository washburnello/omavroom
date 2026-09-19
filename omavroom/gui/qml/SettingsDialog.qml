// Settings view: effective config (from local Config), operator-only
// admission override, and the screenshot polling interval.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "settingsDialog"
    modal: true
    title: "Settings"
    width: 680
    height: 820
    anchors.centerIn: parent

    property string pendingAdmission: backend.admissionOverride
    property string pendingLiveMode: backend.liveMode
    property int pendingThumbnailWidth: backend.thumbnailWidth
    property int pendingFocusedWidth: backend.focusedWidth
    property real pendingFocusedInterval: backend.focusedInterval
    property real pendingWallInterval: backend.wallInterval

    function applyCapture() {
        // Submit the whole set at once: the backend validates and applies it
        // atomically, so a valid cross-field change (e.g. raising thumbnail
        // and focused widths together) is never rejected mid-sequence.
        backend.applyCaptureSettings({
            "live_mode": dialog.pendingLiveMode,
            "thumbnail_width": dialog.pendingThumbnailWidth,
            "focused_width": dialog.pendingFocusedWidth,
            "focused_interval_s": dialog.pendingFocusedInterval,
            "wall_interval_s": dialog.pendingWallInterval
        })
    }

    contentItem: ColumnLayout {
        spacing: 10

        GroupBox {
            Layout.fillWidth: true
            title: "Admission (operator control)"
            RowLayout {
                anchors.fill: parent
                spacing: 10
                Label { text: "Override" }
                ComboBox {
                    id: admissionBox
                    model: ["auto", "allow", "deny"]
                    currentIndex: Math.max(0, model.indexOf(dialog.pendingAdmission))
                    onActivated: dialog.pendingAdmission = currentText
                }
                Label { text: "current: " + backend.admissionOverride; color: "#8b949e" }
                Item { Layout.fillWidth: true }
                Button {
                    text: "Apply"
                    enabled: backend.daemonOk
                    onClicked: backend.setAdmission(dialog.pendingAdmission)
                }
            }
        }

        GroupBox {
            Layout.fillWidth: true
            title: "Golden image source"
            ColumnLayout {
                anchors.fill: parent
                spacing: 4
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Label { text: "Source" }
                    ComboBox {
                        id: goldenBox
                        objectName: "goldenProfileBox"
                        model: [
                            { label: "Stock Omarchy", value: "stock" },
                            { label: "Mirror this machine", value: "mirror" }
                        ]
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: Math.max(0, goldenBox.indexOfValue(backend.goldenProfile))
                        onActivated: backend.setGoldenProfile(currentValue)
                    }
                    Label { text: "current: " + backend.goldenProfile; color: "#8b949e" }
                    Item { Layout.fillWidth: true }
                }
                Label {
                    Layout.fillWidth: true
                    text: "Changing the golden image source requires rebuilding the golden image."
                    color: "#6e7681"
                    wrapMode: Text.Wrap
                }
            }
        }

        GroupBox {
            Layout.fillWidth: true
            title: "Adaptive monitor capture"
            ColumnLayout {
                anchors.fill: parent
                spacing: 6

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Label { text: "Live mode" }
                    ComboBox {
                        id: liveModeBox
                        objectName: "liveModeBox"
                        model: [
                            { label: "Stills", value: "stills" },
                            { label: "VNC (live focused monitor)", value: "vnc" }
                        ]
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: Math.max(0, liveModeBox.indexOfValue(dialog.pendingLiveMode))
                        onActivated: dialog.pendingLiveMode = currentValue
                    }
                    Label {
                        text: backend.liveModeNotice !== "" ? backend.liveModeNotice
                                                             : "current: " + backend.liveMode
                        color: "#8b949e"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Label { text: "Wall thumbnail width" }
                    SpinBox {
                        id: thumbnailBox
                        from: 64
                        to: 4096
                        stepSize: 32
                        value: dialog.pendingThumbnailWidth
                        onValueModified: dialog.pendingThumbnailWidth = value
                    }
                    Label { text: "Focused width" }
                    SpinBox {
                        id: focusedWidthBox
                        from: 64
                        to: 4096
                        stepSize: 64
                        value: dialog.pendingFocusedWidth
                        onValueModified: dialog.pendingFocusedWidth = value
                    }
                    Item { Layout.fillWidth: true }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Label { text: "Focused interval (s)" }
                    DoubleSpinBox {
                        id: focusedIntervalBox
                        from: 0.05
                        to: 30
                        stepSize: 0.05
                        decimals: 2
                        value: dialog.pendingFocusedInterval
                        onValueModified: dialog.pendingFocusedInterval = value
                    }
                    Label { text: "Wall interval (s)" }
                    DoubleSpinBox {
                        id: wallIntervalBox
                        from: 0.05
                        to: 60
                        stepSize: 0.5
                        decimals: 2
                        value: dialog.pendingWallInterval
                        onValueModified: dialog.pendingWallInterval = value
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Apply"
                        onClicked: dialog.applyCapture()
                    }
                }
                Label {
                    Layout.fillWidth: true
                    text: "The wall captures thumbnails on the slow cadence; the focused monitor gets high-res frames on the fast cadence."
                    color: "#6e7681"
                    wrapMode: Text.Wrap
                }
            }
        }

        Label { text: "Effective configuration"; color: "#e6edf3"; font.bold: true }
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            TextArea {
                id: configText
                text: backend.settingsText
                readOnly: true
                wrapMode: TextEdit.NoWrap
                font.family: "monospace"
                font.pixelSize: 12
                selectByMouse: true
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                text: "Images are managed in the local config; admission override is sent to the daemon."
                color: "#6e7681"
                wrapMode: Text.Wrap
            }
            Button { text: "Close"; onClicked: dialog.close() }
        }
    }
}
