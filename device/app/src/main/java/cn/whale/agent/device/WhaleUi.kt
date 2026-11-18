package cn.whale.agent.device

import android.app.Activity
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*

/** Shared native visual language: the screen should speak in user tasks. */
object WhaleUi {
    val background = Color.rgb(244, 248, 246)
    val surface = Color.WHITE
    val ink = Color.rgb(32, 57, 47)
    val muted = Color.rgb(109, 128, 118)
    val accent = Color.rgb(35, 134, 83)
    val soft = Color.rgb(230, 244, 235)
    val line = Color.rgb(225, 235, 229)

    fun dp(context: Context, value: Int) = (context.resources.displayMetrics.density * value).toInt()
    fun shape(color: Int, radius: Float = 20f) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = radius
    }
    fun setup(activity: Activity) {
        activity.actionBar?.hide()
        activity.window.statusBarColor = background
        activity.window.navigationBarColor = surface
        activity.window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or
            View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
    }
    fun column(context: Context, padding: Int = 0) = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        val p = dp(context, padding)
        setPadding(p, p, p, p)
    }
    fun page(context: Context) = column(context, 22).apply {
        setBackgroundColor(WhaleUi.background)
    }
    fun text(context: Context, label: String, size: Float = 14f, color: Int = muted) = TextView(context).apply {
        text = label
        textSize = size
        setTextColor(color)
        setLineSpacing(dp(context, 3).toFloat(), 1f)
        setPadding(0, dp(context, 4), 0, dp(context, 8))
    }
    fun title(context: Context, label: String) = text(context, label, 27f, ink).apply {
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    }
    fun card(context: Context, label: String? = null, hero: Boolean = false) = column(context, 20).apply {
        background = shape(if (hero) accent else surface, dp(context, 22).toFloat())
        elevation = dp(context, 1).toFloat()
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(context, 16) }
        if (label != null) addView(text(context, label, 18f, if (hero) Color.WHITE else ink).apply {
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
    }
    fun input(context: Context, placeholder: String) = EditText(context).apply {
        hint = placeholder
        textSize = 14f
        setTextColor(ink)
        setHintTextColor(muted)
        setPadding(dp(context, 14), dp(context, 10), dp(context, 14), dp(context, 10))
        background = shape(backgroundColor(), dp(context, 12).toFloat())
        minHeight = dp(context, 52)
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(context, 12) }
    }
    private fun backgroundColor() = Color.rgb(245, 249, 246)
    fun button(context: Context, label: String, primary: Boolean = false, action: () -> Unit) = Button(context).apply {
        text = label
        setOnClickListener { action() }
        styleButton(this, primary)
    }
    fun styleButton(button: Button, primary: Boolean = false) {
        val c = button.context
        button.isAllCaps = false
        button.stateListAnimator = null
        button.elevation = 0f
        button.textSize = 14f
        button.minHeight = dp(c, 50)
        button.minimumHeight = dp(c, 50)
        button.setTextColor(ColorStateList(arrayOf(intArrayOf(-android.R.attr.state_enabled), intArrayOf()),
            intArrayOf(muted, if (primary) Color.WHITE else accent)))
        button.background = RippleDrawable(ColorStateList.valueOf(Color.argb(35, 35, 134, 83)),
            shape(if (primary) accent else soft, dp(c, 13).toFloat()), null)
        button.setPadding(dp(c, 12), dp(c, 10), dp(c, 12), dp(c, 10))
        button.layoutParams = LinearLayout.LayoutParams(-1, -2).apply {
            topMargin = dp(c, 5)
            bottomMargin = dp(c, 5)
        }
    }
    fun scroll(context: Context, content: View) = ScrollView(context).apply {
        setBackgroundColor(WhaleUi.background)
        isFillViewport = true
        clipToPadding = false
        addView(content, ViewGroup.LayoutParams(-1, -2))
    }
    fun collapsible(context: Context, label: String, content: View) = column(context).apply {
        addView(button(context, label) {
            content.visibility = if (content.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        })
        content.visibility = View.GONE
        addView(content)
    }
    fun polish(view: View) {
        when (view) {
            is Button -> {
                styleButton(view)
                val parent = view.parent
                if (parent is LinearLayout && parent.orientation == LinearLayout.HORIZONTAL) {
                    view.layoutParams = LinearLayout.LayoutParams(0, -2, 1f).apply {
                        marginEnd = dp(view.context, 6)
                    }
                }
            }
            is EditText -> {
                view.setTextColor(ink)
                view.setHintTextColor(muted)
                view.textSize = 14f
            }
            is TextView -> view.setTextColor(ink)
        }
        if (view is ViewGroup) for (i in 0 until view.childCount) polish(view.getChildAt(i))
    }
}
