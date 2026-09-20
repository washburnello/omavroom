// One permanent monitor slot. Desktop seats render a live downscaled
// screenshot; terminal seats render their live exec text. A torn-down seat
// leaves the tile in place showing "off / no signal".
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: tile
    objectName: "slotTile-" + (slotData.key || "")
    property var slotData: ({})
    //: True while this monitor is the enlarged/focused one.
    property bool focused: false
    //: Live (VNC) image URL for this tile, or "" to show the still thumbnail.
    property string liveSource: ""
    signal peekRequested()
    signal focusRequested()

    readonly property bool occupied: slotData.occupied === true
    readonly property bool desktop: slotData.seat_type === "desktop"
    readonly property bool live: liveSource !== ""
    readonly property string thumb: slotData.thumbnail_source || ""
    //: The still frame is always laid down first; the live image paints over
    //: it only once a real live frame exists. Neither ever blanks the other.
    readonly property bool showStill: desktop && occupied && thumb !== ""
    readonly property string terminal: slotData.terminal_text || ""
    readonly property string offText: slotData.off_text || "off / no signal"
    // Terminals are headless: they have no viewer endpoint, so they must not
    // offer click-to-peek. The backend also rejects it defensively.
    readonly property bool peekable: slotData.peekable === true

    radius: 8
    color: occupied ? "#151a21" : "#0f1216"
    border.width: focused ? 2 : 1
    border.color: focused ? "#388bfd" : (slotData.needsAttention ? "#f85149" : "#262d36")
    clip: true

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        // The "screen": thumbnail, terminal text, or off/no-signal.
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 5
            color: "#04060a"
            border.width: 1
            border.color: "#1c222b"
            clip: true

            // Bottom buffer: the last still. It stays visible underneath the
            // live frame, so the tile is never blank while the live source is
            // empty, loading, or has just fallen back.
            Image {
                id: thumbImage
                anchors.fill: parent
                anchors.margins: 1
                visible: tile.showStill
                source: tile.showStill ? tile.thumb : ""
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                cache: false
                smooth: true
                mipmap: true
            }

            // Top buffer: live VNC frames from the image provider (cache-busted
            // by the live revision). Loaded synchronously so a frame swap is
            // atomic; the still below covers any decode gap. Only shown once a
            // real frame is ready (`liveSource` is "" until then).
            Image {
                id: liveImage
                anchors.fill: parent
                anchors.margins: 1
                visible: tile.live
                source: tile.live ? tile.liveSource : ""
                fillMode: Image.PreserveAspectFit
                asynchronous: false
                cache: false
                smooth: true
                mipmap: true
            }

            Text {
                anchors.fill: parent
                anchors.margins: 8
                visible: !tile.showStill && !tile.live
                text: {
                    if (!tile.occupied)
                        return tile.offText;
                    if (tile.desktop)
                        return (tile.slotData.state || "") + "\n(no signal yet)";
                    return tile.terminal !== "" ? tile.terminal : "idle";
                }
                color: tile.occupied ? "#8b949e" : "#4b535d"
                font.family: tile.desktop ? "sans-serif" : "monospace"
                font.pixelSize: tile.desktop ? 13 : 11
                wrapMode: Text.WrapAnywhere
                elide: Text.ElideRight
                verticalAlignment: Text.AlignTop
            }
        }

        // Header labels: slot name, type, state.
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Label {
                text: slotData.name || ""
                color: "#e6edf3"
                font.bold: true
                elide: Text.ElideRight
            }
            Rectangle {
                color: "#1c222b"
                radius: 3
                implicitWidth: typeLabel.implicitWidth + 10
                implicitHeight: typeLabel.implicitHeight + 2
                Label {
                    id: typeLabel
                    anchors.centerIn: parent
                    text: slotData.seat_type || ""
                    color: "#79c0ff"
                    font.pixelSize: 11
                }
            }
            Item { Layout.fillWidth: true }
            Label {
                text: slotData.state || ""
                color: slotData.needs_attention ? "#f85149" : "#9aa4b2"
            }
        }

        // Identity labels: agent + project.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Label {
                Layout.fillWidth: true
                text: "agent " + (slotData.agent || "-")
                color: "#c9d1d9"
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                text: "project " + (slotData.project || "-")
                color: "#8b949e"
                elide: Text.ElideRight
            }
        }

        // Time labels: elapsed, lease, heartbeat.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Label { text: "elapsed " + (slotData.elapsed || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Label { text: "lease " + (slotData.lease || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Label { text: "hb " + (slotData.heartbeat || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Item { Layout.fillWidth: true }
        }
    }

    HoverHandler { id: hover }
    ToolTip.visible: hover.hovered
    ToolTip.text: focused
        ? "Focused — click to restore the wall"
        : "Click to focus this monitor"

    // A plain click focuses/enlarges this monitor (click again to restore).
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        cursorShape: Qt.PointingHandCursor
        onClicked: tile.focusRequested()
    }

    // Peek is an explicit secondary action (desktop seats only), so a plain
    // click can mean "focus". It appears on hover; nothing opens by itself.
    Button {
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 6
        visible: hover.hovered && tile.occupied && tile.peekable
        text: "Peek"
        font.pixelSize: 11
        padding: 4
        onClicked: tile.peekRequested()
    }
}
